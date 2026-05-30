import * as esbuild from 'esbuild'
import { readFileSync, writeFileSync, mkdirSync, readdirSync, statSync } from 'fs'
import { join, relative, dirname, extname, resolve } from 'path'
import { fileURLToPath } from 'url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const APP_DIR = resolve(__dirname, 'static/app')
const OUT_DIR = resolve(APP_DIR, 'compiled')

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
  const files = collect(APP_DIR)
  mkdirSync(OUT_DIR, { recursive: true })

  for (const file of files) {
    const src = readFileSync(file, 'utf8')
    const result = await esbuild.transform(src, {
      loader: 'jsx',
      jsx: 'transform',
    })

    // Change top-level const to var to avoid redeclaration errors
    // across separate <script> tags (Babel standalone isolated per file)
    let code = result.code.replace(/^const /gm, 'var ')

    // Preserve subdirectory structure relative to APP_DIR
    const rel = relative(APP_DIR, file).replace(/\.jsx$/, '.js')
    const outFile = join(OUT_DIR, rel)
    mkdirSync(dirname(outFile), { recursive: true })
    writeFileSync(outFile, code)
    console.log(`✓ ${relative(APP_DIR, file)} → compiled/${rel}`)
  }

  const total = files.length
  console.log(`\nDone. ${total} JSX file${total > 1 ? 's' : ''} compiled to ${OUT_DIR}`)
}

build().catch(e => {
  console.error('Build failed:', e)
  process.exit(1)
})
