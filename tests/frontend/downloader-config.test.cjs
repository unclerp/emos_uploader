// 执行：node --test tests/frontend/downloader-config.test.cjs
// 加载真实前端脚本，模拟 HTTP 响应，验证表单触发的请求及提示。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function page({ saveError = null, applied = true, deleteApplied = true } = {}) {
    const requests = [], toasts = [];
    const fields = Object.fromEntries(Object.entries({
        dlType: 'qbittorrent', dlName: 'QB2', dlHost: '172.17.0.1', dlPort: '10499',
        dlUser: 'test-user', dlPass: 'test-password', dlRpcUrl: '',
    }).map(([key, value]) => [key, { value }]));
    const sandbox = {
        window: {}, console, localStorage: { getItem: () => 'test-token', removeItem() {} },
        document: { addEventListener() {}, getElementById: id => fields[id] },
        escapeHtml: value => String(value),
        showModal() {}, hideModal() { sandbox.closed = true; },
        showToast(message, kind) { toasts.push({ message, kind }); },
        closed: false,
        async fetch(url, options = {}) {
            const method = options.method || 'GET';
            const body = options.body ? JSON.parse(options.body) : null;
            requests.push({ url, method, body });
            let status = 200;
            let payload = { success: true, applied: true };
            if (method === 'PUT') {
                status = saveError ? 422 : 200;
                payload = saveError ? { detail: saveError } : {
                    success: true, saved: true, applied,
                    message: applied ? '配置已应用' : '配置已保存，但应用失败；旧配置继续运行',
                };
            } else if (method === 'DELETE') {
                // 原 bug 会在新增成功后请求删除空 section。
                if (url === '/api/config/section/') {
                    status = 405;
                    payload = { detail: 'Method Not Allowed' };
                } else {
                    payload = { success: true, applied: deleteApplied, message: '旧配置移除后应用失败' };
                }
            } else if (url === '/api/config') {
                payload = { success: true, config: {} };
            }
            return {
                ok: status >= 200 && status < 300, status,
                headers: { get: () => 'application/json' },
                json: async () => payload,
            };
        },
    };
    vm.createContext(sandbox);
    const root = path.resolve(__dirname, '../../src/video_organizer/web/static');
    vm.runInContext(fs.readFileSync(path.join(root, 'api.js'), 'utf8'), sandbox);
    vm.runInContext(fs.readFileSync(path.join(root, 'app.js'), 'utf8'), sandbox);
    vm.runInContext(`
        el.modalTitle = {};
        el.modalBody = {};
        el.modalConfirmBtn = {};
        el.downloaderConfigList = {};
    `, sandbox);
    return {
        sandbox, fields, requests, toasts,
        submit: () => vm.runInContext('el.modalConfirmBtn.onclick()', sandbox),
    };
}

for (const kind of ['qbittorrent', 'aria2']) {
    test(`新增 ${kind} 只保存新配置，不删除空旧节`, async () => {
        const p = page();
        p.sandbox.showAddDownloaderModal();
        p.fields.dlType.value = kind;
        await p.submit();
        assert.deepEqual(p.requests.map(r => [r.method, r.url]), [
            ['PUT', '/api/config/section'], ['GET', '/api/config'],
        ]);
        assert.equal(p.requests[0].body.section, 'downloader.' + kind);
        assert.equal(p.requests[0].body.values.rpc_url,
            'http://172.17.0.1:10499' + (kind === 'qbittorrent' ? '/api/v2' : '/jsonrpc'));
        assert.equal(p.toasts.at(-1).kind, 'success');
        assert.equal(p.sandbox.closed, true);
    });
}

test('编辑同类型只更新原节', async () => {
    const p = page();
    p.sandbox.editDownloaderConfig({ section: 'downloader.qbittorrent', type: 'qbittorrent' });
    await p.submit();
    assert.equal(p.requests.filter(r => r.method === 'DELETE').length, 0);
    assert.equal(p.toasts.at(-1).kind, 'success');
});

test('修改已有下载器类型，在新配置应用成功后才删除旧节', async () => {
    const p = page();
    p.sandbox.editDownloaderConfig({ section: 'downloader.aria2', type: 'aria2' });
    await p.submit();
    assert.deepEqual(p.requests.map(r => [r.method, r.url]), [
        ['PUT', '/api/config/section'],
        ['DELETE', '/api/config/section/downloader.aria2'],
        ['GET', '/api/config'],
    ]);
    assert.equal(p.toasts.at(-1).kind, 'success');
});

for (const section of ['', 'downloader.aria2']) {
    test(`应用失败保留编辑框和旧配置：${section || '新增'}`, async () => {
        const p = page({ applied: false });
        p.sandbox.editDownloaderConfig({ section, type: 'aria2' });
        await p.submit();
        assert.equal(p.requests.filter(r => r.method === 'DELETE').length, 0);
        assert.equal(p.sandbox.closed, false);
        assert.equal(p.toasts.at(-1).kind, 'error');
        assert.match(p.toasts.at(-1).message, /已保存.*应用失败/);
    });
}

test('实际保存失败时显示服务端错误，不删除旧节', async () => {
    const p = page({ saveError: '配置值无效' });
    p.sandbox.showAddDownloaderModal();
    await p.submit();
    assert.equal(p.requests.length, 1);
    assert.equal(p.sandbox.closed, false);
    assert.equal(p.toasts.at(-1).kind, 'error');
    assert.match(p.toasts.at(-1).message, /配置值无效/);
});

test('删除旧节后的应用失败不会误报成功', async () => {
    const p = page({ deleteApplied: false });
    p.sandbox.editDownloaderConfig({ section: 'downloader.aria2', type: 'aria2' });
    await p.submit();
    assert.equal(p.toasts.at(-1).kind, 'error');
    assert.equal(p.sandbox.closed, false);
});

test('完整 RPC 地址原样提交', async () => {
    const p = page();
    p.fields.dlRpcUrl.value = 'http://172.17.0.1:10499/api/v2';
    p.sandbox.showAddDownloaderModal();
    await p.submit();
    assert.equal(p.requests[0].body.values.rpc_url, p.fields.dlRpcUrl.value);
    assert.equal(p.toasts.at(-1).kind, 'success');
});
