// 用真实 app.js 和可记录节点变动的 DOM 替身验证增量渲染及查询隔离。
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const source = await readFile(new URL('../../src/video_organizer/web/static/app.js', import.meta.url), 'utf8');
const css = await readFile(new URL('../../src/video_organizer/web/static/app.css', import.meta.url), 'utf8');

function setup() {
    const mutations = [];
    let now = Date.parse('2026-11-27T12:00:00Z');
    class Element {
        constructor(tag) {
            this.tagName = tag;
            this.children = [];
            this.parentNode = null;
            this.dataset = {};
            this.style = {};
            this.listeners = {};
            this.value = '';
            this._text = '';
            this._class = '';
            this._title = '';
            this.classList = {
                contains: name => this.className.split(' ').includes(name),
                add: name => { if (!this.classList.contains(name)) this.className += ` ${name}`; },
                remove: name => { this.className = this.className.split(' ').filter(c => c !== name).join(' '); },
            };
        }
        set textContent(value) { mutations.push([this, 'text']); this._text = String(value); }
        get textContent() { return this._text; }
        set className(value) { mutations.push([this, 'class']); this._class = value; }
        get className() { return this._class; }
        set title(value) { mutations.push([this, 'title']); this._title = value; }
        get title() { return this._title; }
        get firstElementChild() { return this.children[0] || null; }
        set innerHTML(_) { throw new Error('最近活动不得用 innerHTML 替换列表'); }
        appendChild(child) { return this.insertBefore(child, null); }
        insertBefore(child, before) {
            if (child.parentNode) child.remove();
            const index = before === null ? this.children.length : this.children.indexOf(before);
            assert.ok(index >= 0);
            this.children.splice(index, 0, child);
            child.parentNode = this;
            mutations.push([this, 'insert']);
            return child;
        }
        remove() {
            if (this.parentNode) {
                mutations.push([this.parentNode, 'remove']);
                this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1);
                this.parentNode = null;
            }
        }
        addEventListener(name, callback) { this.listeners[name] = callback; }
    }
    const nodes = {};
    for (const name of ['recentActivity', 'recentTotal', 'recentPage', 'recentTotalPages', 'recentPrevBtn', 'recentNextBtn', 'recentPagination', 'recentSearchInput']) nodes[name] = new Element('div');
    const context = vm.createContext({
        document: { addEventListener() {}, createElement: tag => new Element(tag), getElementById: () => null },
        window: {},
        console: { error() {} },
        Date: class extends Date { static now() { return now; } },
        AbortController,
        setTimeout: () => 1,
        clearTimeout() {},
        loadRecentActivityPaginated: async () => ({ items: [], total: 0 }),
    });
    vm.runInContext(source, context);
    const state = vm.runInContext('state', context);
    const el = vm.runInContext('el', context);
    Object.assign(el, nodes);
    const view = vm.runInContext('recentActivityView', context);
    return { context, state, view, nodes, mutations, advance: ms => { now += ms; } };
}

const item = (id, overrides = {}) => ({ id, path: `/input/Show.${id}.mkv`, status: 'completed', time: '2026-11-27T11:59:00Z', ...overrides });
const data = (items, total = items.length) => ({ items, total });
const query = (page = 1, pageSize = 20, search = '') => ({ page, pageSize, search });
const drain = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };

export async function runTests() {
    const passed = [];
    async function check(name, body) { await body(); passed.push(name); }

    await check('相同结果不重建节点、不改行内容', () => {
        const { context, nodes, mutations } = setup();
        context.renderRecentActivity(data([item(1), item(2)]), query());
        const first = nodes.recentActivity.children[0];
        mutations.length = 0;
        context.renderRecentActivity(data([item(1), item(2)]), query());
        assert.equal(nodes.recentActivity.children[0], first);
        assert.equal(mutations.length, 0);
    });

    await check('状态变化只更新原行的状态及颜色', () => {
        const { context, nodes, mutations } = setup();
        context.renderRecentActivity(data([item(1, { status: 'uploading' })]), query());
        const row = nodes.recentActivity.children[0];
        mutations.length = 0;
        context.renderRecentActivity(data([item(1)]), query());
        assert.equal(nodes.recentActivity.children[0], row);
        assert.equal(row.activityCells.status.textContent, '已完成');
        assert.ok(mutations.every(([node]) => [row.activityCells.dot, row.activityCells.status].includes(node)));
        assert.equal(row.classList.contains('activity-item-new'), false);
    });

    await check('第一页仅新增条目动画，旧条目复用并按返回顺序排列', () => {
        const { context, nodes } = setup();
        context.renderRecentActivity(data([item(1), item(2)]), query());
        const old = nodes.recentActivity.children[0];
        context.renderRecentActivity(data([item(3), item(1)], 3), query());
        const [added, existing] = nodes.recentActivity.children;
        assert.equal(existing, old);
        assert.equal(added.classList.contains('activity-item-new'), true);
        assert.equal(existing.classList.contains('activity-item-new'), false);
        added.listeners.animationend();
        assert.equal(added.classList.contains('activity-item-new'), false);
    });

    await check('筛选新增匹配条目使用重新查询的结果，不依赖推送第一页', async () => {
        const { context, state, nodes } = setup();
        state.recentSearch = '目标剧';
        const currentQuery = query(1, 20, '目标剧');
        context.renderRecentActivity(data([item(1)]), currentQuery);
        const old = nodes.recentActivity.children[0];
        const calls = [];
        context.loadRecentActivityPaginated = async (...args) => { calls.push(args); return data([item(99), item(1)]); };
        context.handleDashboardUpdate({ type: 'update', recent: data([item(123, { path: '/其他剧.mkv' })]) });
        await drain();
        assert.deepEqual(calls[0].slice(0, 3), [1, 20, '目标剧']);
        assert.equal(nodes.recentActivity.children[0].dataset.activityKey, 'task:99');
        assert.equal(nodes.recentActivity.children[0].classList.contains('activity-item-new'), true);
        assert.equal(nodes.recentActivity.children[1], old);
        assert.equal(state.recentSearch, '目标剧');
    });

    await check('不匹配的新增活动不会让筛选列表重绘', async () => {
        const { context, state, mutations } = setup();
        state.recentSearch = '目标剧';
        context.renderRecentActivity(data([item(1)]), query(1, 20, '目标剧'));
        context.loadRecentActivityPaginated = async () => data([item(1)]);
        mutations.length = 0;
        context.refreshRecentActivityFromPush(data([item(99)]));
        await drain();
        assert.equal(mutations.length, 0);
    });

    await check('第 2 页保留页码、每页数量和搜索词，不使用推送默认页', async () => {
        const { context, state, nodes } = setup();
        Object.assign(state, { recentPage: 2, recentPageSize: 50, recentSearch: 'Show' });
        context.renderRecentActivity(data([item(51)], 150), query(2, 50, 'Show'));
        let args;
        context.loadRecentActivityPaginated = async (...values) => { args = values; return data([item(50), item(51)], 151); };
        context.handleDashboardUpdate({ type: 'update', recent: data([item(999)]) });
        await drain();
        assert.deepEqual(args.slice(0, 3), [2, 50, 'Show']);
        assert.equal(state.recentPage, 2);
        assert.equal(nodes.recentPage.textContent, '2');
        assert.equal(nodes.recentTotalPages.textContent, '4');
        assert.equal(nodes.recentActivity.children[0].classList.contains('activity-item-new'), false);
    });

    await check('慢搜索请求不能覆盖后来选择的筛选条件', async () => {
        const { context, state, nodes } = setup();
        const first = deferred();
        context.loadRecentActivityPaginated = (_, __, search) => search === '旧搜索' ? first.promise : Promise.resolve(data([item(2)]));
        state.recentSearch = '旧搜索';
        const old = context.updateRecentActivity();
        state.recentSearch = '新搜索';
        await context.updateRecentActivity();
        first.resolve(data([item(1)]));
        await old;
        assert.equal(nodes.recentActivity.children[0].dataset.activityKey, 'task:2');
        assert.equal(state.recentSearch, '新搜索');
    });

    await check('旧分页请求晚返回不能把当前页改回去', async () => {
        const { context, state, nodes } = setup();
        const first = deferred();
        context.loadRecentActivityPaginated = page => page === 1 ? first.promise : Promise.resolve(data([item(21)], 50));
        const old = context.updateRecentActivity();
        state.recentPage = 2;
        await context.updateRecentActivity();
        first.resolve(data([item(1)], 50));
        await old;
        assert.equal(state.recentPage, 2);
        assert.equal(nodes.recentActivity.children[0].dataset.activityKey, 'task:21');
    });

    await check('旧 HTTP 结果不能覆盖默认第一页的新推送', async () => {
        const { context, nodes } = setup();
        const first = deferred();
        context.loadRecentActivityPaginated = () => first.promise;
        const old = context.updateRecentActivity();
        context.refreshRecentActivityFromPush(data([item(2)]));
        first.resolve(data([item(1)]));
        await old;
        assert.equal(nodes.recentActivity.children[0].dataset.activityKey, 'task:2');
    });

    await check('连续推送合并补查，不反复取消慢请求', async () => {
        const { context, state, nodes } = setup();
        state.recentSearch = 'Show';
        const first = deferred();
        let calls = 0;
        let firstSignal;
        context.loadRecentActivityPaginated = (_, __, ___, signal) => {
            calls++;
            if (calls === 1) { firstSignal = signal; return first.promise; }
            return Promise.resolve(data([item(2), item(1)]));
        };
        const request = context.updateRecentActivity();
        context.refreshRecentActivityFromPush(data([item(90)]));
        context.refreshRecentActivityFromPush(data([item(91)]));
        assert.equal(calls, 1);
        assert.equal(firstSignal.aborted, false);
        first.resolve(data([item(1)]));
        await request;
        await drain();
        assert.equal(calls, 2);
        assert.equal(nodes.recentActivity.children[0].dataset.activityKey, 'task:2');
    });

    await check('暂时请求失败保留已显示条目', async () => {
        const { context, nodes } = setup();
        context.renderRecentActivity(data([item(1)]), query());
        const row = nodes.recentActivity.children[0];
        context.loadRecentActivityPaginated = async () => { throw new Error('offline'); };
        await context.updateRecentActivity();
        assert.equal(nodes.recentActivity.children[0], row);
    });

    await check('切换筛选后请求失败不能继续冒充上一筛选结果', async () => {
        const { context, state, nodes } = setup();
        context.renderRecentActivity(data([item(1)]), query());
        state.recentSearch = '不同筛选';
        context.loadRecentActivityPaginated = async () => { throw new Error('offline'); };
        await context.updateRecentActivity();
        assert.equal(nodes.recentActivity.firstElementChild.className, 'empty-state');
        assert.equal(nodes.recentActivity.firstElementChild.firstElementChild.textContent, '加载失败');
        assert.equal(state.recentSearch, '不同筛选');
    });

    await check('断线轮询与推送共用补查机制，不会取消同条件慢请求', async () => {
        const { context, nodes } = setup();
        const first = deferred();
        let calls = 0;
        let signal;
        context.loadRecentActivityPaginated = (_, __, ___, value) => {
            calls++;
            if (calls === 1) { signal = value; return first.promise; }
            return Promise.resolve(data([item(2)]));
        };
        context.requestRecentActivityRefresh();
        context.requestRecentActivityRefresh();
        context.requestRecentActivityRefresh();
        assert.equal(calls, 1);
        assert.equal(signal.aborted, false);
        first.resolve(data([item(1)]));
        await drain();
        assert.equal(calls, 2);
        assert.equal(nodes.recentActivity.children[0].dataset.activityKey, 'task:2');
    });

    await check('搜索或翻页切换不把整页旧记录当新增动画', () => {
        const { context, nodes } = setup();
        context.renderRecentActivity(data([item(1)]), query());
        context.renderRecentActivity(data([item(2)]), query(1, 20, '新筛选'));
        assert.equal(nodes.recentActivity.children[0].classList.contains('activity-item-new'), false);
    });

    await check('同名不同任务保留独立节点，路径文本不作为 HTML 执行', () => {
        const { context, nodes } = setup();
        const path = '<img src=x onerror=alert(1)>.mkv';
        context.renderRecentActivity(data([item(1, { path }), item(2, { path })]), query());
        assert.equal(nodes.recentActivity.children.length, 2);
        assert.notEqual(...nodes.recentActivity.children);
        assert.equal(nodes.recentActivity.children[0].activityCells.path.textContent, path);
    });

    await check('相对时间变化只改时间文字', () => {
        const { context, nodes, mutations, advance } = setup();
        context.renderRecentActivity(data([item(1)]), query());
        const row = nodes.recentActivity.children[0];
        mutations.length = 0;
        advance(60000);
        context.refreshRecentActivityTimes();
        assert.equal(row.activityCells.time.textContent, '2分钟前');
        assert.equal(mutations.length, 1);
        assert.equal(mutations[0][0], row.activityCells.time);
    });

    await check('重复空结果保留同一空状态节点', () => {
        const { context, nodes, mutations } = setup();
        context.renderRecentActivity(data([]), query());
        const empty = nodes.recentActivity.firstElementChild;
        mutations.length = 0;
        context.renderRecentActivity(data([]), query());
        assert.equal(nodes.recentActivity.firstElementChild, empty);
        assert.equal(mutations.length, 0);
    });

    await check('基础条目没有入场动画，动画仅绑定新增标记', () => {
        const base = css.match(/\.activity-item\s*\{([^}]+)\}/)[1];
        assert.doesNotMatch(base, /animation\s*:/);
        assert.match(css, /\.activity-item-new\s*\{[^}]*animation\s*:/);
    });

    return passed;
}
