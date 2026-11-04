import { readFile } from 'node:fs/promises'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react(), {
    name: 'font-licenses',
    apply: 'build',
    async generateBundle() {
      for (const font of ['ibm-plex-sans', 'jetbrains-mono']) {
        const path = new URL('./node_modules/@fontsource/' + font + '/LICENSE', import.meta.url)
        this.emitFile({ type: 'asset', fileName: 'licenses/' + font + '.txt', source: await readFile(path, 'utf8') })
      }
    },
  }],
  server: { proxy: { '/api': 'http://127.0.0.1:8000' } },
})
