import * as esbuild from 'esbuild'
import { readFileSync, writeFileSync, mkdirSync, readdirSync, statSync, existsSync } from 'fs'
import { join, relative, dirname, extname, resolve } from 'path'
import { fileURLToPath } from 'url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const APP_DIR = resolve(__dirname, 'static/app')
const OUT_DIR = resolve(APP_DIR, 'compiled')
const WORKBENCH_DIR = resolve(__dirname, '../workbench-webui')

function collect(dir) {
  const files = []
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) {
      files.push(...collect(full))
    } else if (entry.endsWith('.jsx')) {
      files.push(full)
    }
  }
  return files
}

async function build() {
  const workbenchFiles = existsSync(WORKBENCH_DIR) ? collect(WORKBENCH_DIR) : []
  const files = [...collect(APP_DIR), ...workbenchFiles]
  mkdirSync(OUT_DIR, { recursive: true })

  for (const file of files) {
    const srcDir = file.startsWith(WORKBENCH_DIR) ? WORKBENCH_DIR : APP_DIR
    const rel = relative(srcDir, file).replace(/\.jsx$/, '.js')
    const outFile = join(OUT_DIR, rel)
    mkdirSync(dirname(outFile), { recursive: true })

    if (rel === 'code/editor.js') {
      await esbuild.build({
        entryPoints: [file],
        outfile: outFile,
        bundle: true,
        format: 'iife',
        platform: 'browser',
        jsx: 'transform',
        target: 'es2020',
        logLevel: 'silent',
      })
    } else {
      const src = readFileSync(file, 'utf8')
      const result = await esbuild.transform(src, {
        loader: 'jsx',
        jsx: 'transform',
      })

      // Change top-level const to var to avoid redeclaration errors
      // across separate <script> tags (Babel standalone isolated per file)
      const code = result.code.replace(/^const /gm, 'var ')
      writeFileSync(outFile, code)
    }

    console.log(`✓ ${relative(srcDir, file)} → compiled/${rel}`)
  }

  const total = files.length
  console.log(`\nDone. ${total} JSX file${total > 1 ? 's' : ''} compiled to ${OUT_DIR}`)
}

build().catch(e => {
  console.error('Build failed:', e)
  process.exit(1)
})
