"""Codebase indexer — builds and queries a structured index of the codebase.

Stores symbols (functions, classes), references (call graph), and imports
in a SQLite database for fast semantic queries by the agent.
"""

import ast
import asyncio
import hashlib
import json
import logging
import sqlite3
import time
from pathlib import Path

from cyrene.config import WORKSPACE_DIR

logger = logging.getLogger(__name__)

INDEX_DB = WORKSPACE_DIR.parent / "data" / "code_index.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    hash TEXT,
    size INTEGER,
    indexed_at REAL
);

CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES files(id),
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    line INTEGER,
    end_line INTEGER,
    class_name TEXT DEFAULT '',
    signature TEXT DEFAULT '',
    docstring TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS refs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_symbol_id INTEGER REFERENCES symbols(id),
    source_file_id INTEGER NOT NULL REFERENCES files(id),
    caller_name TEXT DEFAULT '',
    target_name TEXT NOT NULL,
    line INTEGER,
    kind TEXT DEFAULT 'call'
);

CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES files(id),
    module TEXT NOT NULL,
    imported_names TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_id);
CREATE INDEX IF NOT EXISTS idx_refs_target ON refs(target_name);
CREATE INDEX IF NOT EXISTS idx_refs_file ON refs(source_file_id);
CREATE INDEX IF NOT EXISTS idx_imports_module ON imports(module);
"""

_schema_ensured = False


def _ensure_schema() -> None:
    global _schema_ensured
    if _schema_ensured:
        return
    INDEX_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(INDEX_DB))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()
    _schema_ensured = True


def _connect() -> sqlite3.Connection:
    _ensure_schema()
    conn = sqlite3.connect(str(INDEX_DB))
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _hash_file(path: Path) -> str:
    """Compute MD5 hash of a file using chunked reading."""
    try:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _compute_rel_path(file_path: Path, project_root: Path) -> str | None:
    """Compute relative path, handling symlink edge cases."""
    try:
        rp = file_path.resolve()
        pr = project_root.resolve()
        return str(rp.relative_to(pr))
    except (ValueError, OSError):
        return None


# ── AST Extraction ──

class _Extractor(ast.NodeVisitor):
    """Extract symbols, references, and imports from a Python AST.

    Collects all Call nodes once at the file level and assigns them to
    the enclosing function/method afterward, avoiding O(n²) re-walks.
    """

    def __init__(self, file_id: int, source: str):
        self.file_id = file_id
        self.source_lines = source.splitlines()
        self.symbols: list[dict] = []
        self.refs: list[dict] = []
        self.imports: list[dict] = []
        self._current_class = ""
        self._symbol_line_ranges: dict[str, tuple[int, int]] = {}
        self._all_calls: list[dict] = []

    def _docstring(self, node) -> str:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            text = ast.get_docstring(node)
            return text[:200] if text else ""
        return ""

    def _qualified_name(self, name: str) -> str:
        if self._current_class:
            return f"{self._current_class}.{name}"
        return name

    def visit_ClassDef(self, node):
        prev_class = self._current_class
        self._current_class = node.name
        qname = node.name
        self.symbols.append({
            "name": qname,
            "kind": "class",
            "line": node.lineno,
            "end_line": node.end_lineno or node.lineno,
            "class_name": "",
            "signature": f"class {node.name}",
            "docstring": self._docstring(node),
        })
        self._symbol_line_ranges[qname] = (node.lineno, node.end_lineno or node.lineno)
        self.generic_visit(node)
        self._current_class = prev_class

    def _visit_func(self, node, is_async: bool):
        prefix = "async " if is_async else ""
        args = [a.arg for a in node.args.args]
        qname = self._qualified_name(node.name)
        sig = f"{prefix}def {node.name}({', '.join(args)})"
        kind = "method" if self._current_class else "function"
        self.symbols.append({
            "name": qname,
            "kind": kind,
            "line": node.lineno,
            "end_line": node.end_lineno or node.lineno,
            "class_name": self._current_class,
            "signature": sig,
            "docstring": self._docstring(node),
        })
        self._symbol_line_ranges[qname] = (node.lineno, node.end_lineno or node.lineno)
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        self._visit_func(node, is_async=False)

    def visit_AsyncFunctionDef(self, node):
        self._visit_func(node, is_async=True)

    def visit_Call(self, node):
        target = None
        receiver = ""
        if isinstance(node.func, ast.Name):
            target = node.func.id
        elif isinstance(node.func, ast.Attribute):
            target = node.func.attr
            if isinstance(node.func.value, ast.Name):
                receiver = node.func.value.id
            elif isinstance(node.func.value, ast.Attribute):
                receiver = node.func.value.attr
        if target:
            self._all_calls.append({
                "target_name": target,
                "receiver": receiver,
                "line": node.lineno,
            })
        self.generic_visit(node)

    def visit_Import(self, node):
        for alias in node.names:
            self.imports.append({
                "module": alias.name,
                "imported_names": alias.asname or alias.name,
            })

    def visit_ImportFrom(self, node):
        if node.module:
            names = ", ".join(a.name for a in node.names)
            self.imports.append({
                "module": node.module,
                "imported_names": names,
            })

    def _assign_calls_to_symbols(self):
        """Post-pass: assign each call to the enclosing function/method by line range."""
        for call in self._all_calls:
            owner = ""
            for qname, (start, end) in self._symbol_line_ranges.items():
                if start <= call["line"] <= end:
                    owner = qname
                    break
            self.refs.append({
                "caller_name": owner,
                "target_name": call["target_name"],
                "receiver": call["receiver"],
                "line": call["line"],
                "kind": "call",
            })


# ── Indexing ──

def _index_file(conn: sqlite3.Connection, file_path: Path) -> dict | None:
    """Index a single Python file. Returns dict or None if file is gone."""
    project_root = WORKSPACE_DIR.parent
    rel_path = _compute_rel_path(file_path, project_root)
    if rel_path is None:
        return None

    file_hash = _hash_file(file_path)
    if not file_hash:
        return None  # file unreadable or gone

    cur = conn.execute("SELECT id, hash FROM files WHERE path = ?", (rel_path,))
    row = cur.fetchone()
    if row and row[1] == file_hash:
        return {"path": rel_path, "symbols": 0, "refs": 0, "imports": 0, "skipped": True}

    try:
        source = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    if row:
        file_id = row[0]
        conn.execute("DELETE FROM symbols WHERE file_id = ?", (file_id,))
        conn.execute("DELETE FROM refs WHERE source_file_id = ?", (file_id,))
        conn.execute("DELETE FROM imports WHERE file_id = ?", (file_id,))
    else:
        cur = conn.execute(
            "INSERT INTO files (path, hash, size, indexed_at) VALUES (?, ?, ?, ?)",
            (rel_path, file_hash, len(source), time.time()),
        )
        file_id = cur.lastrowid

    conn.execute(
        "UPDATE files SET hash = ?, size = ?, indexed_at = ? WHERE id = ?",
        (file_hash, len(source), time.time(), file_id),
    )

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {"path": rel_path, "symbols": 0, "refs": 0, "imports": 0, "error": "syntax_error"}

    extractor = _Extractor(file_id, source)
    extractor.visit(tree)
    extractor._assign_calls_to_symbols()

    # Insert symbols in batch
    sym_data = [
        (file_id, s["name"], s["kind"], s["line"], s["end_line"],
         s["class_name"], s["signature"], s["docstring"])
        for s in extractor.symbols
    ]
    conn.executemany(
        "INSERT INTO symbols (file_id, name, kind, line, end_line, class_name, signature, docstring) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        sym_data,
    )

    # Insert references
    ref_data = [
        (file_id, r["caller_name"], r["target_name"], r["line"], r["kind"])
        for r in extractor.refs
    ]
    conn.executemany(
        "INSERT INTO refs (source_file_id, caller_name, target_name, line, kind) "
        "VALUES (?, ?, ?, ?, ?)",
        ref_data,
    )

    # Insert imports
    imp_data = [
        (file_id, i["module"], i["imported_names"])
        for i in extractor.imports
    ]
    conn.executemany(
        "INSERT INTO imports (file_id, module, imported_names) VALUES (?, ?, ?)",
        imp_data,
    )

    return {
        "path": rel_path,
        "symbols": len(extractor.symbols),
        "refs": len(extractor.refs),
        "imports": len(extractor.imports),
        "skipped": False,
    }


def _collect_py_files(root: Path) -> list[Path]:
    exclude = {"__pycache__", ".venv", ".git", "node_modules", "dist", "build", ".tox", "venv", "env"}
    files = []
    for path in root.rglob("*.py"):
        if any(p in exclude for p in path.parts):
            continue
        files.append(path)
    return files


def build_index(path: str = ".", force: bool = False) -> dict:
    project_root = WORKSPACE_DIR.parent
    candidate = Path(path)
    resolved = (project_root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()

    pr_resolved = project_root.resolve()
    if pr_resolved not in resolved.parents and resolved != pr_resolved:
        return {"error": f"Path outside project root: {path}"}
    if not resolved.exists():
        return {"error": f"Path not found: {path}"}

    conn = _connect()
    try:
        if force:
            conn.execute("DELETE FROM refs")
            conn.execute("DELETE FROM symbols")
            conn.execute("DELETE FROM imports")
            conn.execute("DELETE FROM files")

        if resolved.is_file():
            py_files = [resolved]
        elif resolved.is_dir():
            py_files = _collect_py_files(resolved)
        else:
            return {"error": f"Not a file or directory: {path}"}

        # Collect current file paths for stale-entry cleanup
        current_paths = set()
        for fp in py_files:
            rp = _compute_rel_path(fp, project_root)
            if rp:
                current_paths.add(rp)

        results = []
        for fp in py_files:
            result = _index_file(conn, fp)
            if result is not None:
                results.append(result)

        # Clean up stale entries for deleted files
        if not force:
            cur = conn.execute("SELECT id, path FROM files")
            for row in cur.fetchall():
                if row[1] not in current_paths:
                    conn.execute("DELETE FROM symbols WHERE file_id = ?", (row[0],))
                    conn.execute("DELETE FROM refs WHERE source_file_id = ?", (row[0],))
                    conn.execute("DELETE FROM imports WHERE file_id = ?", (row[0],))
                    conn.execute("DELETE FROM files WHERE id = ?", (row[0],))

        conn.commit()

        total_files = sum(1 for r in results if not r.get("skipped"))
        total_symbols = sum(r.get("symbols", 0) for r in results)
        total_refs = sum(r.get("refs", 0) for r in results)
        total_imports = sum(r.get("imports", 0) for r in results)
        skipped = sum(1 for r in results if r.get("skipped"))

        cur = conn.execute("SELECT COUNT(*) FROM files")
        db_files = cur.fetchone()[0]
        cur = conn.execute("SELECT COUNT(*) FROM symbols")
        db_symbols = cur.fetchone()[0]
    finally:
        conn.close()

    return {
        "status": "ok",
        "files_scanned": len(py_files),
        "files_indexed": total_files,
        "files_skipped": skipped,
        "symbols_found": total_symbols,
        "refs_found": total_refs,
        "imports_found": total_imports,
        "db_total_files": db_files,
        "db_total_symbols": db_symbols,
    }


# ── Query functions ──

def search_symbol(name: str, kind: str = "") -> dict:
    conn = _connect()
    try:
        query = (
            "SELECT s.name, s.kind, s.class_name, s.signature, s.line, s.end_line, s.docstring, f.path "
            "FROM symbols s JOIN files f ON s.file_id = f.id "
            "WHERE s.name LIKE ?"
        )
        params = [f"%{name}%"]
        if kind:
            query += " AND s.kind = ?"
            params.append(kind)
        query += " ORDER BY s.name, f.path LIMIT 50"
        cur = conn.execute(query, params)
        results = []
        for row in cur.fetchall():
            results.append({
                "name": row[0], "kind": row[1], "class_name": row[2] or "",
                "signature": row[3] or "", "line": row[4],
                "end_line": row[5] or row[4], "docstring": row[6] or "", "file": row[7],
            })
    finally:
        conn.close()
    return {"status": "ok", "results": results, "count": len(results)}


def find_references(name: str) -> dict:
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT r.target_name, r.caller_name, r.line, r.kind, f.path "
            "FROM refs r JOIN files f ON r.source_file_id = f.id "
            "WHERE r.target_name LIKE ? "
            "ORDER BY f.path, r.line LIMIT 100",
            (f"%{name}%",),
        )
        refs = []
        for row in cur.fetchall():
            refs.append({
                "target": row[0], "caller": row[1], "line": row[2],
                "kind": row[3], "file": row[4],
            })
    finally:
        conn.close()
    return {"status": "ok", "results": refs, "count": len(refs)}


def get_file_symbols(path: str) -> dict:
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT s.name, s.kind, s.class_name, s.signature, s.line, s.end_line, s.docstring "
            "FROM symbols s JOIN files f ON s.file_id = f.id "
            "WHERE f.path = ? "
            "ORDER BY s.line",
            (path,),
        )
        symbols = []
        for row in cur.fetchall():
            symbols.append({
                "name": row[0], "kind": row[1], "class_name": row[2] or "",
                "signature": row[3] or "", "line": row[4],
                "end_line": row[5] or row[4], "docstring": row[6] or "",
            })
    finally:
        conn.close()
    return {"status": "ok", "file": path, "symbols": symbols, "count": len(symbols)}


# ── Tool handlers (wrapped in asyncio.to_thread to avoid blocking) ──

async def _tool_index_codebase(args: dict, bot=None, chat_id=None, db_path=None, notify_state=None) -> str:
    path = str(args.get("path", "."))
    force = bool(args.get("force", False))
    result = await asyncio.to_thread(build_index, path, force)
    return json.dumps(result, ensure_ascii=False)


async def _tool_search_symbol(args: dict, bot=None, chat_id=None, db_path=None, notify_state=None) -> str:
    name = str(args.get("name", ""))
    kind = str(args.get("kind", ""))
    if not name:
        return json.dumps({"error": "name is required"}, ensure_ascii=False)
    result = await asyncio.to_thread(search_symbol, name, kind)
    return json.dumps(result, ensure_ascii=False)


async def _tool_find_references(args: dict, bot=None, chat_id=None, db_path=None, notify_state=None) -> str:
    name = str(args.get("name", ""))
    if not name:
        return json.dumps({"error": "name is required"}, ensure_ascii=False)
    result = await asyncio.to_thread(find_references, name)
    return json.dumps(result, ensure_ascii=False)


async def _tool_get_file_symbols(args: dict, bot=None, chat_id=None, db_path=None, notify_state=None) -> str:
    path = str(args.get("path", ""))
    if not path:
        return json.dumps({"error": "path is required"}, ensure_ascii=False)
    result = await asyncio.to_thread(get_file_symbols, path)
    return json.dumps(result, ensure_ascii=False)


# ── Tool definitions ──

INDEX_CODEBASE_DEF = {
    "type": "function",
    "function": {
        "name": "IndexCodebase",
        "description": (
            "Build or update the code index for the project. Scans Python files and extracts "
            "symbols (functions, classes, methods), call-graph references, and imports into a "
            "SQLite database. Use this before SearchSymbol/FindReferences/GetFileSymbols. "
            "Use force=True to rebuild from scratch and clean stale entries."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory or file to index (default: '.' for project root)."},
                "force": {"type": "boolean", "description": "Force full rebuild (default: false, incremental)."},
            },
            "required": [],
        },
    },
}

SEARCH_SYMBOL_DEF = {
    "type": "function",
    "function": {
        "name": "SearchSymbol",
        "description": (
            "Search for functions, classes, or methods by name (partial match on the qualified "
            "name like 'ClassName.method_name'). Returns file path, line range, signature, and "
            "docstring. Results capped at 50 — narrow the search if the count reaches 50. "
            "Requires IndexCodebase to have been run first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Symbol name to search (partial match). Example: 'handle_login'."},
                "kind": {"type": "string", "description": "Optional filter: 'function', 'class', or 'method'."},
            },
            "required": ["name"],
        },
    },
}

FIND_REFERENCES_DEF = {
    "type": "function",
    "function": {
        "name": "FindReferences",
        "description": (
            "Find all call sites that reference a given function or method name (partial match). "
            "Shows the caller, target, file, and line number. Results capped at 100. "
            "Requires IndexCodebase to have been run first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Target name. Example: 'verify_password' finds all callers."},
            },
            "required": ["name"],
        },
    },
}

GET_FILE_SYMBOLS_DEF = {
    "type": "function",
    "function": {
        "name": "GetFileSymbols",
        "description": (
            "Get all symbols (functions, classes, methods) defined in a specific file with "
            "their signatures and docstrings. Quick structural overview without reading the "
            "entire file. Requires IndexCodebase to have been run first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Exact file path as stored in the index."},
            },
            "required": ["path"],
        },
    },
}


def register_to(tool_defs: list, tool_handlers: dict) -> None:
    tool_defs.append(INDEX_CODEBASE_DEF)
    tool_handlers["IndexCodebase"] = _tool_index_codebase
    tool_defs.append(SEARCH_SYMBOL_DEF)
    tool_handlers["SearchSymbol"] = _tool_search_symbol
    tool_defs.append(FIND_REFERENCES_DEF)
    tool_handlers["FindReferences"] = _tool_find_references
    tool_defs.append(GET_FILE_SYMBOLS_DEF)
    tool_handlers["GetFileSymbols"] = _tool_get_file_symbols
