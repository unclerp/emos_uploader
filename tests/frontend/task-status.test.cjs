// 使用真实前端脚本验证 HTTP 首屏、WebSocket 更新和分页状态。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function page(items) {
    const requests = [], timers = [];
    const sandbox = {
        window: {}, console, URLSearchParams,
        localStorage: { getItem: () => 'test', removeItem() {} },
        document: { addEventListener() {}, getElementById: () => null },
        escapeHtml: value => String(value), formatSize: value => String(value),
        showToast() {}, showModal() {},
        setTimeout: callback => timers.push(callback),
        async fetch(url) {
            requests.push(url);
            return { ok: true, headers: { get: () => 'application/json' },
                json: async () => ({ items, total: items.length, page: 1, page_size: 20 }) };
        },
    };
    vm.createContext(sandbox);
    const root = path.resolve(__dirname, '../../src/video_organizer/web/static');
    for (const script of ['api.js', 'app.js']) {
        vm.runInContext(fs.readFileSync(path.join(root, script), 'utf8'), sandbox);
    }
    vm.runInContext(`
        el.recentActivity = {}; el.taskList = {}; el.uploadProgressList = {};
        el.clearFailedBtn = {style:{}}; el.retryAllBtn = {style:{}};
        el.taskIssueFilter = {value:'failed'};
    `, sandbox);
    return { requests, timers, run: code => vm.runInContext(code, sandbox) };
}

for (const [status, label, tone] of [
    ['queued', '队列中', 'warning'], ['processing', '处理中', 'warning'],
    ['uploading', '上传中', 'warning'], ['completed', '已完成', 'success'],
    ['failed', '上传失败', 'danger'], ['skipped', '已跳过', 'muted'],
]) {
    for (const source of ['HTTP', 'WebSocket']) {
        test(`${source} 最近活动 ${label} 使用 ${tone}`, async () => {
            const item = { status, path: '/input/film.mkv', failure_count: 0 };
            const p = page([item]);
            if (source === 'HTTP') await p.run('updateRecentActivity()');
            else p.run(`handleDashboardUpdate({recent:{items:${JSON.stringify([item])},total:1}})`);
            const html = p.run('el.recentActivity.innerHTML');
            assert.match(html, new RegExp('activity-dot-' + tone));
            assert.match(html, new RegExp('activity-status-' + tone));
            assert.ok(html.includes(label));
        });
    }
}

test('已跳过标签请求独立分页状态，不混入已完成', async () => {
    const p = page([{id: 1, path: '/input/film.mkv', status: 'skipped', targets: 'emos',
        attempt_count: 1, failure_count: 0, error: '资源已存在，重复上传已跳过'}]);
    await p.run("state.currentTab = 'skipped'; updateTaskList()");
    assert.ok(p.requests[0].includes('status=skipped'));
    assert.match(p.run('el.taskList.innerHTML'), /已跳过/);
    assert.doesNotMatch(p.run('el.taskList.innerHTML'), /已完成|上传失败|retryTrackedTask/);
});

test('排重进度展示已跳过并在终态后移除', () => {
    const p = page([]);
    p.run(`handleUploadProgressUpdate({file_path:'/input/film.mkv',status:'skipped',progress:100})`);
    assert.match(p.run('el.uploadProgressList.innerHTML'), /已跳过/);
    assert.doesNotMatch(p.run('el.uploadProgressList.innerHTML'), /已完成|上传中/);
    assert.equal(p.timers.length, 1);
    p.timers[0]();
    assert.equal(p.run('Object.keys(uploadState.progresses).length'), 0);
});

test('失败后确认资源已存在，显示已跳过及已解决', () => {
    const p = page([]);
    assert.equal(p.run("taskStatusLabel({status:'skipped',resolved:true})"), '已跳过 · 已解决');
});
