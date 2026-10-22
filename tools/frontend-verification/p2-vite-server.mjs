import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

// Load the actual source config while keeping generated Vite caches inside the tooling owner.
const [source, cacheDir, port] = process.argv.slice(2)
process.chdir(source)
const { createServer } = await import(pathToFileURL(join(source, 'node_modules/vite/dist/node/index.js')).href)
const { default: factory } = await import(pathToFileURL(join(source, 'vite.config.js')).href)
const config = typeof factory === 'function' ? await factory({ command: 'serve', mode: 'development', isSsrBuild: false, isPreview: false }) : factory
const server = await createServer({ ...config, configFile: false, root: source, cacheDir, server: { ...config.server, host: '127.0.0.1', port: Number(port), strictPort: true, open: false } })
for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, async () => { await server.close(); process.exit(0) })
await server.listen()
