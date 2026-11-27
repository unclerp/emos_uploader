// 运行真实 app.js，用假网络和时钟验证漏消息、重连及请求乱序。
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const source = await readFile(new URL('../../src/video_organizer/web/static/app.js', import.meta.url), 'utf8');

function setup() {
    const timeouts = new Map();
    const intervals = [];
    let timerId = 0;
    let now = 100000;
    const sockets = [];
    class Socket {
        constructor() { sockets.push(this); }
        send() {}
        close() { this.onclose?.(); }
        message(data) { this.onmessage({ data: JSON.stringify(data) }); }
    }
    const context = vm.createContext({
        document: { addEventListener() {}, querySelector: () => ({}) },
        window: {},
        location: { protocol: 'http:', host: 'localhost' },
        console: { error() {} },
        Date: class extends Date { static now() { return now; } },
        AbortController,
        WebSocket: Socket,
        setTimeout: (fn, ms) => { const id = ++timerId; timeouts.set(id, { fn, ms }); return id; },
        clearTimeout: id => timeouts.delete(id),
        setInterval: fn => { intervals.push(fn); return intervals.length; },
        clearInterval() {},
        getAuthToken: () => null,
        loadUploadProgressFromApi: async () => ({ success: true, count: 0, progress: {} }),
        escapeHtml: value => String(value),
        formatSize: value => String(value),
    });
    vm.runInContext(source, context);
    const state = vm.runInContext('uploadState', context);
    const appState = vm.runInContext('state', context);
    vm.runInContext('el.uploadProgressList = { innerHTML: "" }; el.uploadStatusDot = {}; dashboardState.isConnected = true;', context);
    return { context, state, appState, timeouts, intervals, sockets, advance: ms => { now += ms; } };
}

const active = value => ({ filename: 'example.mkv', uploader: 'emos', progress: value, status: 'uploading' });
const snapshot = (progress = {}) => ({ type: 'snapshot', success: true, count: Object.keys(progress).length, progress });

export async function runTests() {
    const passed = [];
    async function check(name, body) { await body(); passed.push(name); }

    await check('REST 的 progress 空快照清除残留条目', async () => {
        const { context, state } = setup();
        state.progresses['/example.mkv'] = active(63);
        await context.loadUploadProgress();
        assert.equal(Object.keys(state.progresses).length, 0);
        assert.match(vm.runInContext('el.uploadProgressList.innerHTML', context), /暂无上传任务/);
    });

    await check('重连后的完整快照替换旧条目并保留当前上传', async () => {
        const { context, state, sockets } = setup();
        state.progresses['/old.mkv'] = active(63);
        context.connectUploadProgressWebSocket();
        sockets[0].message(snapshot({ '/new.mkv': active(20) }));
        assert.deepEqual(Object.keys(state.progresses), ['/new.mkv']);
    });

    await check('迟到的 HTTP 响应不能复活已被快照清除的上传', async () => {
        const { context, state } = setup();
        let resolve;
        context.loadUploadProgressFromApi = () => new Promise(done => { resolve = done; });
        const request = context.loadUploadProgress();
        context.applyUploadProgressSnapshot(snapshot());
        resolve(snapshot({ '/example.mkv': active(63) }));
        await request;
        assert.equal(Object.keys(state.progresses).length, 0);
    });

    await check('旧完成事件的计时器不能清除同一路径的新重试', async () => {
        const { context, state, timeouts } = setup();
        context.handleUploadProgressUpdate({ file_path: '/example.mkv', status: 'completed', progress: 100 });
        const cleanup = [...timeouts.values()].find(t => t.ms === 5000);
        context.handleUploadProgressUpdate({ file_path: '/example.mkv', ...active(10) });
        cleanup.fn();
        assert.equal(state.progresses['/example.mkv'].progress, 10);
    });

    await check('WebSocket 半断开时定期 HTTP 校准', async () => {
        const { context, state, intervals, advance } = setup();
        let calls = 0;
        context.loadUploadProgressFromApi = async () => { calls++; return snapshot(); };
        context.loadTasks = () => {};
        state.isConnected = true;
        state.lastSnapshotAt = 100000;
        context.startAutoRefresh();
        intervals[0]();
        assert.equal(calls, 0);
        advance(10000);
        intervals[0]();
        await Promise.resolve();
        assert.equal(calls, 1);
    });

    await check('HTTP 失败保留真实进度且下一次能继续校准', async () => {
        const { context, state } = setup();
        state.progresses['/example.mkv'] = active(63);
        context.loadUploadProgressFromApi = async () => { throw new Error('offline'); };
        await context.loadUploadProgress();
        assert.equal(state.progresses['/example.mkv'].progress, 63);
        assert.equal(state.snapshotLoading, false);
        context.loadUploadProgressFromApi = async () => snapshot();
        await context.loadUploadProgress();
        assert.equal(Object.keys(state.progresses).length, 0);
    });

    await check('未结束的 HTTP 请求不会重复发起，超时后可恢复', async () => {
        const { context, state, timeouts } = setup();
        let calls = 0;
        context.loadUploadProgressFromApi = signal => {
            calls++;
            return new Promise((_, reject) => signal.addEventListener('abort', () => reject(new Error('timeout'))));
        };
        const request = context.loadUploadProgress();
        await context.loadUploadProgress();
        assert.equal(calls, 1);
        [...timeouts.values()].find(t => t.ms === 10000).fn();
        await request;
        assert.equal(state.snapshotLoading, false);
        context.loadUploadProgressFromApi = async () => snapshot();
        await context.loadUploadProgress();
        assert.equal(Object.keys(state.progresses).length, 0);
    });

    await check('主动关闭不触发重连，旧连接消息不覆盖新连接', async () => {
        const { context, state, sockets } = setup();
        context.connectUploadProgressWebSocket();
        const old = sockets[0];
        context.disconnectUploadProgressWebSocket();
        assert.equal(state.reconnectTimer, null);
        context.connectUploadProgressWebSocket();
        state.progresses['/new.mkv'] = active(20);
        old.message(snapshot());
        assert.equal(state.progresses['/new.mkv'].progress, 20);
        assert.equal(state.ws, sockets[1]);
    });

    return passed;
}
