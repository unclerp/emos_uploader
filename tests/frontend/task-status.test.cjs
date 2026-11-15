// 使用真实前端脚本验证 HTTP 首屏、WebSocket 更新和分页状态。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function page(items) {
    const requests = [], timers = [], methods = [];
    const sandbox = {
        window: {}, console, URLSearchParams,
        localStorage: { getItem: () => 'test', removeItem() {} },
        document: { addEventListener() {}, getElementById: () => null },
        setTimeout: callback => timers.push(callback),
        async fetch(url, options = {}) {
            requests.push(url);
            methods.push(options.method || 'GET');
            return { ok: true, headers: { get: () => 'application/json' },
                json: async () => ({ items, total: items.length, page: 1, page_size: 20 }) };
        },
    };
    vm.createContext(sandbox);
    const root = path.resolve(__dirname, '../../src/video_organizer/web/static');
    for (const script of ['api.js', 'ui.js', 'rules.js', 'app.js']) {
        vm.runInContext(fs.readFileSync(path.join(root, script), 'utf8'), sandbox);
    }
    Object.assign(sandbox, { showToast() {}, showModal() {} });
    vm.runInContext(`
        el.recentActivity = {}; el.taskList = {}; el.uploadProgressList = {};
        el.clearFailedBtn = {style:{}}; el.retryAllBtn = {style:{}};
        el.taskIssueFilter = {value:'failed'};
    `, sandbox);
    return { requests, timers, methods, run: code => vm.runInContext(code, sandbox) };
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


test('上传后操作列表独立展示失败，按钮只重试移动', async () => {
    const p = page([{id:7, torrent_name:'Season',torrent_hash:'abc',kind:'move',destination:'/qb/archive',status:'failed',error:'目录不可写',attempt_count:1}]);
    await p.run("state.currentTab = 'post-upload'; updateTaskList()");
    assert.ok(p.requests[0].startsWith('/api/tasks/post-upload?'));
    assert.match(p.run('el.taskList.innerHTML'), /上传完成，移动失败/);
    assert.match(p.run('el.taskList.innerHTML'), /仅重试移动/);
    assert.doesNotMatch(p.run('el.taskList.innerHTML'), /retryTrackedTask|上传失败/);
    await p.run('retryPostUploadAction(7)');
    assert.equal(p.requests[1], '/api/tasks/post-upload/7/retry');
    assert.equal(p.methods[1], 'POST');
    assert.ok(p.requests.every(url => !url.startsWith('/api/tasks/retry')));
});

test('最近活动保留上传结果，移动失败另行说明', () => {
    const p = page([]);
    assert.equal(p.run("taskStatusLabel({status:'completed',post_upload:{status:'failed',kind:'move'}})"), '上传完成，移动失败');
    assert.equal(p.run("taskStatusLabel({status:'skipped',post_upload:{status:'failed',kind:'move'}})"), '已跳过，移动失败');
});

for (const [action,legacy,expected] of [['',true,'delete'],['',false,'keep'],['keep',true,'keep'],['move',false,'move']]) {
    test(`上传后操作下拉框兼容旧值 ${legacy} 并优先显示 ${action || '兼容设置'}`, () => {
        const p = page([]);
        p.run(`state.config={processing:{delete_after_upload:${legacy},post_upload_action:${JSON.stringify(action)},post_upload_move_path:'/qb/archive'}}`);
        const html = p.run(`renderConfigField('processing','post_upload_action',${JSON.stringify(action)})`);
        assert.match(html, /<select[^>]+name="processing.post_upload_action"/);
        assert.match(html, new RegExp('value="' + expected + '" selected'));
        assert.match(html, /保留原文件/);
        assert.match(html, /通过 qB 移动/);
        assert.match(html, /删除任务及文件/);
        assert.match(html, /aria-describedby="cfg-processing-post_upload_action-help"/);
        assert.doesNotMatch(html, /<small>/);
        assert.equal(p.run("configEntries('processing').some(([k])=>k==='delete_after_upload')"), false);
    });
}

test('Telegram 页面显示独立通知开关和明确的额外频道说明', () => {
    const p = page([]);
    for (const [key,label] of [['compact_mode','批量简洁模式'],['notify_queue','队列总览'],['auto_pin_queue','自动置顶队列总览'],['notify_success','上传成功及重试解决'],['notify_failure','上传或识别失败'],['notify_skipped','重复资源跳过'],['notify_post_upload','上传后移动／删除']]) {
        const html=p.run(`renderConfigField('telegram',${JSON.stringify(key)},true)`);
        assert.ok(html.includes(label));
        assert.ok(html.includes(`name="telegram.${key}"`));
    }
    for (const [key, description] of [
        ['enabled', '需同时填写 Bot Token'],
        ['thread_id', '仅群组话题需要填写'],
        ['compact_mode', '总览保持 5 秒刷新'],
        ['notify_queue', '每轮独立总览'],
        ['auto_pin_queue', '置顶新一轮总览后'],
    ]) {
        const html = p.run(`renderConfigField('telegram',${JSON.stringify(key)},'')`);
        const helpId = `cfg-telegram-${key}-help`;
        assert.ok(html.includes(`aria-describedby="${helpId}">?</button>`));
        assert.ok(html.includes(`id="${helpId}" class="field-help-tooltip" role="tooltip" popover="auto">${description}`));
        assert.ok(!html.includes('<small>'));
    }
});

test('网盘和求片有独立的表单字段与浮层提示', () => {
    const p = page([]);
    for (const [section, key, text] of [
        ['telegram_cloud', 'channel_chat_id', '填写后将网盘链接和秒传信息发到该频道'],
        ['telegram_cloud', 'notify_recycle', '回收站清理结果'],
        ['telegram_cloud', 'notify_links', '网盘链接和秒传信息'],
        ['telegram_seek', 'enabled', '首次成功查询只建立快照'],
        ['telegram_seek', 'poll_interval', '默认 120 秒'],
    ]) {
        const html = p.run(`renderConfigField('${section}','${key}',true)`);
        assert.ok(html.includes(`name="${section}.${key}"`));
        assert.ok(html.includes(text));
    }
});

test('Telegram Token 字段保持密码输入，话题 ID 可留空', () => {
    const p=page([]);
    assert.match(p.run("renderConfigField('telegram','bot_token','')"),/type="password"/);
    assert.match(p.run("renderConfigField('telegram','thread_id','')"),/普通聊天、群组或频道请留空/);
});

test('手动规则切换编辑模式后，填法说明仍关联当前字段', () => {
    const p = page([]);
    const list = p.run(`
        const host = {
            querySelector(selector) { return selector === '[data-rule-warnings]' ? {} : {checked: true}; },
            querySelectorAll() { return []; },
        };
        ManualRulesEditor.render(host, {enabled: true});
        host.innerHTML;
    `);
    assert.match(list, /aria-describedby="manual-rule-keyword-help"/);
    assert.doesNotMatch(list, /placeholder="留空匹配全部文件"/);
    const text = p.run(`
        host.onclick({target: {closest: () => ({dataset: {action: 'mode'}})}});
        host.innerHTML;
    `);
    assert.match(text, /aria-describedby="manual-rules-text-help"/);
    assert.match(text, /popover="auto">每行一条规则/);
});
