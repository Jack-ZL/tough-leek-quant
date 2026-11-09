// Node 22+ 自带实验性 localStorage 全局 getter（未传 --localstorage-file 时返回 undefined），
// 该属性不可枚举，vitest 的 jsdom 环境按可枚举键复制 window 属性时不会覆盖它，
// 导致测试里 localStorage/sessionStorage 恒为 undefined。这里用内存实现兜底；
// 若运行环境已提供可用实现（浏览器/旧 Node + jsdom），则不做任何事。
const createStorage = (): Storage => {
	let store = new Map<string, string>()
	return {
		get length() { return store.size },
		clear() { store = new Map() },
		getItem(key) { return store.get(key) ?? null },
		setItem(key, value) { store.set(key, String(value)) },
		removeItem(key) { store.delete(key) },
		key(index) { return [...store.keys()][index] ?? null },
	}
}

if (globalThis.localStorage === undefined) {
	Object.defineProperty(globalThis, 'localStorage', { value: createStorage(), configurable: true, writable: true })
}
if (globalThis.sessionStorage === undefined) {
	Object.defineProperty(globalThis, 'sessionStorage', { value: createStorage(), configurable: true, writable: true })
}

// 同理, jsdom 的 matchMedia 挂在 Window.prototype 上, 不会被复制到测试全局;
// theme.ts 等处直接调用 window.matchMedia, 缺失时整个 Layout 渲染即崩。补一个最小实现。
if (typeof globalThis.matchMedia !== 'function') {
	Object.defineProperty(globalThis, 'matchMedia', {
		value: (query: string): MediaQueryList => ({
			matches: false,
			media: query,
			onchange: null,
			addListener: () => {},
			removeListener: () => {},
			addEventListener: () => {},
			removeEventListener: () => {},
			dispatchEvent: () => false,
		}),
		configurable: true,
		writable: true,
	})
}
