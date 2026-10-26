// 手动规则列表与高级文本共用草稿，保存时统一输出原有 DSL。
const ManualRulesEditor = (() => {
    let host, rows = [], mode = 'list', flags = {};
    const simple = /^(?:when\s*:\s*(.*?)\s*=>\s*)?\{\[tmdbid=(\d+);type=(tv|movie)\]\}$/i;
    const disabledPrefix = '# disabled: ';
    const escape = value => escapeHtml(String(value ?? '')).replaceAll('"', '&quot;').replaceAll("'", '&#39;');
    function decode(item) {
        const text = item.rule;
        const match = simple.exec(text.trim());
        return match ? {kind:'tmdb', keyword:match[1] || '', id:match[2], type:match[3].toLowerCase(), enabled:item.enabled !== false} :
            {kind:'raw', rule:text, enabled:item.enabled !== false};
    }
    function compile(row, index) {
        if (row.kind === 'raw') {
            if (!row.rule.trim()) throw new Error(`第 ${index + 1} 条规则不能为空`);
            return {rule:row.rule.trim(), enabled:row.enabled};
        }
        const keyword = row.keyword.trim();
        if (/[\r\n]|=>/.test(keyword)) throw new Error(`第 ${index + 1} 条关键词不能包含换行或 =>`);
        if (!/^\d+$/.test(row.id) || !Number.isSafeInteger(Number(row.id)) || Number(row.id) < 1) throw new Error(`第 ${index + 1} 条需要有效的 TMDB 正整数编号`);
        return {rule:`${keyword ? 'when: ' + keyword + ' => ' : ''}{[tmdbid=${row.id};type=${row.type}]}`, enabled:row.enabled};
    }
    function sync() {
        flags.enabled = host.querySelector('[data-flag="enabled"]').checked;
        flags.normalize_symbols = host.querySelector('[data-flag="normalize_symbols"]').checked;
        if (mode === 'text') {
            rows = host.querySelector('[data-rules-text]').value.split(/\r?\n/).map(line => line.trim()).filter(Boolean).map(line =>
                decode({rule:line.startsWith(disabledPrefix) ? line.slice(disabledPrefix.length) : line, enabled:!line.startsWith(disabledPrefix)}));
        } else {
            host.querySelectorAll('[data-rule-index]').forEach(node => {
                const row = rows[Number(node.dataset.ruleIndex)];
                node.querySelectorAll('[data-field]').forEach(input => {
                    row[input.dataset.field] = input.type === 'checkbox' ? input.checked : input.value;
                });
            });
        }
    }
    function conflicts() {
        const warnings = [];
        for (let i = 0; i < rows.length; i++) for (let j = i + 1; j < rows.length; j++) {
            const a = rows[i], b = rows[j];
            if (!a.enabled || !b.enabled || a.kind !== 'tmdb' || b.kind !== 'tmdb') continue;
            const x = a.keyword.trim().toLowerCase(), y = b.keyword.trim().toLowerCase();
            if (!x || !y || x.includes(y) || y.includes(x)) warnings.push(`第 ${i+1}、${j+1} 条匹配条件可能重叠，后面的规则会覆盖前面的指定信息。`);
        }
        const panel = host.querySelector('[data-rule-warnings]');
        if (panel) panel.textContent = warnings.join(' ');
    }
    function rowHtml(row, i) {
        const input = (field, value, label, extra = '') => `<input class="form-input" data-field="${field}" value="${escape(value)}" aria-label="第 ${i+1} 条${label}" autocomplete="off" ${extra}>`;
        const fields = row.kind === 'tmdb' ?
            `<td>${input('keyword',row.keyword,'匹配关键词','placeholder="留空匹配全部文件"')}</td><td>${input('id',row.id,'TMDB 编号','type="number" min="1" step="1"')}</td><td><select class="form-select" data-field="type" aria-label="第 ${i+1} 条类型"><option value="tv" ${row.type==='tv'?'selected':''}>电视剧</option><option value="movie" ${row.type==='movie'?'selected':''}>电影</option></select></td>` :
            `<td colspan="3"><label>高级规则（保留原文）<textarea class="form-input" data-field="rule" aria-label="第 ${i+1} 条高级规则" rows="2" spellcheck="false">${escape(row.rule)}</textarea></label></td>`;
        return `<tr data-rule-index="${i}"><td>${i+1}</td>${fields}<td><input type="checkbox" data-field="enabled" aria-label="第 ${i+1} 条启用" ${row.enabled?'checked':''}></td><td><div style="display:flex;gap:6px;flex-wrap:wrap"><button type="button" class="btn btn-sm btn-secondary" data-action="up" data-index="${i}" ${i===0?'disabled':''} aria-label="上移第 ${i+1} 条">↑</button><button type="button" class="btn btn-sm btn-secondary" data-action="down" data-index="${i}" ${i===rows.length-1?'disabled':''} aria-label="下移第 ${i+1} 条">↓</button><button type="button" class="btn btn-sm btn-secondary" data-action="delete" data-index="${i}" aria-label="删除第 ${i+1} 条">删除</button></div></td></tr>`;
    }
    function paint() {
        const text = mode === 'text' ? rows.map(compile).map(row => (row.enabled ? '' : disabledPrefix) + row.rule).join('\n') : '';
        host.innerHTML = `<div class="config-section"><div class="config-section-title">手动规则</div>
            <div style="display:flex;gap:24px;flex-wrap:wrap;margin:16px 0"><label><input type="checkbox" data-flag="enabled" ${flags.enabled?'checked':''}> 启用手动规则</label><label><input type="checkbox" data-flag="normalize_symbols" ${flags.normalize_symbols?'checked':''}> 规范化符号</label></div>
            <div style="display:flex;gap:12px;margin:16px 0">${mode==='list'?'<button type="button" class="btn btn-secondary" data-action="add">新增规则</button>':''}<button type="button" class="btn btn-secondary" data-action="mode">${mode==='list'?'高级文本模式':'列表模式'}</button></div>
            <p style="color:var(--text-muted)">按从上到下执行，后面的规则可以覆盖前面的结果。编辑完成后点击右上角“保存配置”。</p>
            <p role="status" data-rule-warnings style="color:var(--accent-warning,#e6a23c)"></p>
            ${mode==='text'?`<textarea class="form-input" data-rules-text rows="12" spellcheck="false" aria-label="高级规则文本">${escape(text)}</textarea><p style="color:var(--text-muted)">每行一条规则；停用规则以 ${disabledPrefix} 开头。</p>`:
                `<div class="config-table-wrap"><table class="config-table" style="min-width:720px"><thead><tr><th>#</th><th>匹配关键词</th><th>TMDB 编号</th><th>类型</th><th>启用</th><th>操作</th></tr></thead><tbody>${rows.map(rowHtml).join('')}</tbody></table></div>${rows.length?'':'<p>暂无规则，点击“新增规则”开始添加。</p>'}`}
            </div>`;
        conflicts();
    }
    function render(container, config) {
        host = container;
        rows = (config.rules || []).map(item => decode(typeof item === 'string' ? {rule:item,enabled:true} : item));
        flags = {enabled:!!config.enabled, normalize_symbols:config.normalize_symbols !== false};
        mode = 'list';
        paint();
        host.oninput = host.onchange = () => { sync(); conflicts(); };
        host.onclick = event => {
            const button = event.target.closest('button[data-action]');
            if (!button) return;
            try {
                sync();
                const action = button.dataset.action, index = Number(button.dataset.index);
                if (action === 'add') rows.push({kind:'tmdb',keyword:'',id:'',type:'tv',enabled:true});
                if (action === 'delete') rows.splice(index,1);
                if (action === 'up' && index>0) [rows[index-1],rows[index]]=[rows[index],rows[index-1]];
                if (action === 'down' && index<rows.length-1) [rows[index+1],rows[index]]=[rows[index],rows[index+1]];
                if (action === 'mode') { if (mode === 'list') rows.map(compile); mode = mode === 'list' ? 'text' : 'list'; }
                paint();
            } catch (error) { showToast(error.message, 'error'); }
        };
    }
    function read() { sync(); return {...flags, rules:rows.map(compile)}; }
    function detach() {
        if (host) host.oninput = host.onchange = host.onclick = null;
        host = null;
    }
    return {render,read,detach};
})();
