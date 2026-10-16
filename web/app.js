'use strict';
const $ = id => document.getElementById(id);
let data, period = 'month', visibleProjects = [], visibleExpenses = [], toastTimer, expenseRequest, voidExpense;
const nf = new Intl.NumberFormat('zh-CN');
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const compact = value => value >= 1e9 ? (value/1e9).toFixed(2)+' B' : value >= 1e6 ? (value/1e6).toFixed(2)+' M' : value >= 1e3 ? (value/1e3).toFixed(1)+' K' : nf.format(value);
const fmtTime = value => value ? new Intl.DateTimeFormat('zh-CN',{timeZone:data?.config.timezone || 'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}).format(new Date(value)) : '尚无记录';
function toast(message) { $('toast').textContent=message; $('toast').hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').hidden=true,6000); }
async function api(path, body) {
  const options = body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-Dashboard-Nonce':document.querySelector('meta[name="dashboard-nonce"]').content},body:JSON.stringify(body)};
  const response=await fetch(path,options); const result=await response.json();
  if(!response.ok) {const error=new Error(result.error || '请求失败');error.duplicate=result.duplicate;throw error;} return result;
}
async function load() { data=await api('/api/data'); if(!$('month').value) $('month').value=data.now.slice(0,7); render(); }
function range() {
  const today=data.now.slice(0,10);
  if(period==='today') return [today,today];
  if(period==='week') {const day=new Date(today+'T12:00:00Z');day.setUTCDate(day.getUTCDate()-6);return [day.toISOString().slice(0,10),today];}
  if(period==='month') {const month=$('month').value;const year=Number(month.slice(0,4)), m=Number(month.slice(5,7));return [month+'-01',month+'-'+new Date(Date.UTC(year,m,0)).getUTCDate()];}
  const dates=[today,...data.rows.map(r=>r.day),...(data.expenses||[]).map(e=>e.payment_date)].sort();return [dates[0],dates.at(-1)];
}
function money(amount) {return new Intl.NumberFormat('zh-CN',{style:'currency',currency:data.config.currency,maximumFractionDigits:2}).format(amount);}
function render() {
  const [from,to]=range();const rows=data.rows.filter(r=>r.day>=from&&r.day<=to);
  const sum=key=>rows.reduce((a,r)=>a+r[key],0); const total=sum('total_tokens'), input=sum('input_tokens'), cached=sum('cached_input_tokens');
  const projects=new Map(),models=new Map(),days=new Map(),sessions=new Set();
  for(const r of rows) {
    if(!projects.has(r.project)) projects.set(r.project,{key:r.project,name:r.name,attribution:r.attribution,total:0,cached:0,output:0,sessions:new Set()});
    const p=projects.get(r.project);p.total+=r.total_tokens;p.cached+=r.cached_input_tokens;p.output+=r.output_tokens;
    r.sessions.forEach(s=>{p.sessions.add(s);sessions.add(s);});
    models.set(r.model,(models.get(r.model)||0)+r.total_tokens);days.set(r.day,(days.get(r.day)||0)+r.total_tokens);
  }
  $('total').textContent=compact(total);$('total').title=nf.format(total)+' Token';
  $('total-note').textContent='输入 '+compact(input)+' · 输出 '+compact(sum('output_tokens'));
  $('cache').textContent=input?(cached/input*100).toFixed(1)+'%':'—';$('cache-note').textContent='缓存输入 '+compact(cached)+' · 已包含在输入量中';
  $('projects').textContent=projects.size;$('session-note').textContent=sessions.size+' 个会话有用量记录';
  $('fee').textContent=data.config.subscription_monthly===null?'待填写':money(data.config.subscription_monthly);
  $('fee-note').textContent=data.config.subscription_monthly===null?'填入实际月费后，按 Token 占比分摊':'内部预算 · 非自动读取账单 · 不含额外付费';
  $('month').hidden=period!=='month';$('range-label').textContent=from+' — '+to;
  $('allocation-note').textContent=period==='month'?'分摊 = 月费 × 项目 Token 占比。缓存输入等权；不是官方账单。':'订阅分摊请切换至「按月」查看；其他范围展示真实用量记录。';
  $('project-count').textContent=projects.size;
  const fee=data.config.subscription_monthly;
  visibleProjects=[...projects.values()].sort((a,b)=>b.total-a.total).map(p=>({...p,share:total?p.total/total:0,cost:period==='month'&&fee!==null&&total?fee*p.total/total:null}));
  renderProjects();renderTrend(days,from,to);renderModels(models,total);renderQuota();renderHealth();renderExpenses();
}

function expenseMoney(minor,currency) {const digits=data.currency_digits[currency];return currency+' '+new Intl.NumberFormat('zh-CN',{minimumFractionDigits:digits,maximumFractionDigits:digits}).format(minor/10**digits);}
function expenseGroups(rows,key) {
  const groups=new Map();for(const row of rows){if(!groups.has(row[key]))groups.set(row[key],new Map());const amounts=groups.get(row[key]);amounts.set(row.currency,(amounts.get(row.currency)||0)+row.signed_minor);}
  return [...groups].sort((a,b)=>a[0].localeCompare(b[0],'zh-CN')).map(([name,amounts])=>`<div class="info-row"><span>${escapeHtml(name)}</span><span>${escapeHtml([...amounts].map(([currency,minor])=>expenseMoney(minor,currency)).join(' · '))}</span></div>`).join('');
}
function renderExpenses() {
  const [from,to]=range(),term=$('expense-search').value.trim().toLowerCase();
  const selected=$('expense-currency').value,currencies=[...new Set((data.expenses||[]).map(e=>e.currency))].sort();
  $('expense-currency').innerHTML='<option value="">所有币种</option>'+currencies.map(c=>`<option>${c}</option>`).join('');$('expense-currency').value=currencies.includes(selected)?selected:'';
  const matching=(data.expenses||[]).filter(e=>e.payment_date>=from&&e.payment_date<=to&&(!selected||e.currency===selected)&&[e.payee,e.description,e.project,e.category,e.note].join(' ').toLowerCase().includes(term));
  const active=matching.filter(e=>!e.voided_at);visibleExpenses=matching.filter(e=>!e.voided_at||$('expense-history').checked);
  $('expense-count').textContent=active.length+' 笔有效记录';
  const totals=new Map();for(const e of active){if(!totals.has(e.currency))totals.set(e.currency,{paid:0,refund:0,count:0});const sum=totals.get(e.currency);sum[e.kind==='refund'?'refund':'paid']+=e.amount_minor;sum.count++;}
  $('expense-totals').innerHTML=[...totals].sort().map(([c,t])=>`<article class="expense-total"><div>当前范围净支出 · ${c}</div><strong>${escapeHtml(expenseMoney(t.paid-t.refund,c))}</strong><p>付款 ${escapeHtml(expenseMoney(t.paid,c))} · 退款 ${escapeHtml(expenseMoney(t.refund,c))}</p></article>`).join('')||'<div class="expense-no-total">尚无可汇总的实际支出。各币种会分别显示，不自动换算汇率。</div>';
  $('expense-breakdown').innerHTML=active.length?`<details><summary>按分类与项目查看汇总</summary><div class="expense-form-grid"><div><h3>费用分类</h3>${expenseGroups(active,'category')}</div><div><h3>所属项目</h3>${expenseGroups(active,'project')}</div></div></details>`:'';
  $('expense-rows').innerHTML=visibleExpenses.map(e=>`<tr class="${e.voided_at?'voided':''}"><td><div class="project-title">${escapeHtml(e.payment_date)} · ${escapeHtml(e.payee)}</div><div class="project-meta">${escapeHtml(e.description)}</div>${e.note?`<div class="project-meta">备注：${escapeHtml(e.note)}</div>`:''}<div class="project-meta">编号 ${e.id}</div></td><td>${escapeHtml(e.category)}</td><td>${escapeHtml(e.project)}</td><td class="${e.kind==='refund'?'refund':'cost'}">${escapeHtml(expenseMoney(e.signed_minor,e.currency))}</td><td>${e.voided_at?'已作废':e.kind==='refund'?'退款':'已支付'}${e.voided_at?`<div class="project-meta">${escapeHtml(e.void_reason)}</div>`:''}</td><td>${e.voided_at?'—':`<button class="void-button" data-void-expense="${e.id}">作废</button>`}</td></tr>`).join('');
  $('expense-empty').hidden=visibleExpenses.length>0;
  $('expense-empty').textContent=(data.expenses||[]).length?'当前日期或筛选条件下没有匹配记录。':'还没有实际支出记录。以后把付款信息发在这段聊天里，或点击「记一笔」。';
  $('expense-project-options').innerHTML=[...new Set(data.rows.map(r=>r.name))].sort().map(name=>`<option value="${escapeHtml(name)}"></option>`).join('');
}
function renderProjects() {
  const term=$('search').value.trim().toLowerCase();const projects=visibleProjects.filter(p=>p.name.toLowerCase().includes(term));
  $('project-rows').innerHTML=projects.map(p=>`<tr><td><div class="project-cell"><span class="project-icon">${escapeHtml(p.name.slice(0,1))}</span><div><div class="project-title" title="${escapeHtml(p.name)}">${escapeHtml(p.name)}</div><div class="project-meta">${escapeHtml(p.attribution)} · ${p.sessions.size} 个会话</div></div></div></td><td title="${nf.format(p.total)}">${compact(p.total)}</td><td>${compact(p.cached)}</td><td>${compact(p.output)}</td><td><div class="share"><div class="track"><div class="fill" style="width:${p.share*100}%"></div></div>${(p.share*100).toFixed(1)}%</div></td><td class="cost">${p.cost===null?'—':money(p.cost)}</td></tr>`).join('');
  $('empty').hidden=projects.length>0;
}
function renderTrend(days,from,to) {
  const all=[];let cursor=new Date(from+'T12:00:00Z');const last=new Date(to+'T12:00:00Z');
  for(let i=0;cursor<=last&&i<3660;i++,cursor.setUTCDate(cursor.getUTCDate()+1)) {const day=cursor.toISOString().slice(0,10);all.push({day,total:days.get(day)||0});}
  // Long histories are binned into at most 60 bars; each tooltip states its interval.
  const stride=Math.max(1,Math.ceil(all.length/60));const bins=[];
  for(let i=0;i<all.length;i+=stride) {const group=all.slice(i,i+stride);bins.push({day:group[0].day,end:group.at(-1).day,total:group.reduce((s,r)=>s+r.total,0)});}
  const max=Math.max(1,...bins.map(d=>d.total)), w=600,h=118, left=43,plot=545,step=plot/Math.max(1,bins.length);
  let svg='<svg viewBox="0 0 600 166" role="img" aria-label="每日用量柱状图">';
  for(let i=0;i<3;i++){const y=12+i*h/2;svg+=`<line x1="${left}" x2="590" y1="${y}" y2="${y}" stroke="#edf0e8"/><text x="0" y="${y+4}">${compact(max*(1-i/2))}</text>`;}
  bins.forEach((b,i)=>{const bh=b.total/max*h;svg+=`<rect x="${left+i*step+step*.16}" y="${12+h-bh}" width="${Math.max(1,step*.68)}" height="${bh}" rx="2" fill="${i===bins.length-1?'#d6aa64':'#547c70'}"><title>${b.day}${b.end!==b.day?' 至 '+b.end:''}：${nf.format(b.total)} Token</title></rect>`;});
  svg+=`<text x="${left}" y="155">${from.slice(5)}</text><text text-anchor="end" x="590" y="155">${to.slice(5)}</text></svg>`;
  $('trend').innerHTML=bins.length?svg:'<div class="empty">暂无记录</div>';$('trend-range').textContent=stride===1?'DAILY TOKENS':stride+' DAYS / BAR';
}
function renderModels(models,total) {
  const sorted=[...models].sort((a,b)=>b[1]-a[1]);const top=sorted.slice(0,4);if(sorted.length>4)top.push(['其他模型',sorted.slice(4).reduce((s,r)=>s+r[1],0)]);
  $('models').innerHTML=top.map(([name,n],i)=>`<div class="model-row"><div class="model-label"><b>${escapeHtml(name==='unknown'?'模型信息缺失':name)}</b><span>${(n/total*100).toFixed(1)}% · ${compact(n)}</span></div><div class="track"><div class="fill" style="width:${n/total*100}%;background:${['#214b4c','#729885','#d3af71','#b2bea7','#bccac3'][i]}"></div></div></div>`).join('')||'<p class="loading-note">当前范围内没有模型用量。</p>';
}
function renderQuota() {
  const q=data.quota;if(!q){$('quota').innerHTML='<p class="loading-note">本地记录中暂无账号额度快照。</p>';return;}
  let html='';for(const [key,name] of [['primary','主要额度窗口'],['secondary','次要额度窗口']]){
    const limit=q.limits[key];if(!limit)continue;const used=limit.used_percent;const duration=limit.window_minutes;
    const title=duration===10080?'每周额度':duration===300?'5 小时额度':name;
    html+=`<div class="quota-row"><span>${title}</span><strong>${used===null||used===undefined?'未知':used+'% 已用'}</strong></div><div class="track"><div class="fill" style="width:${Math.min(100,Math.max(0,used||0))}%"></div></div><div class="quota-caption">${limit.resets_at?'重置时间：'+fmtTime(limit.resets_at*1000):'重置时间未知'}</div>`;
  }
  const age=(new Date()-new Date(q.timestamp))/3600000;
  html+=`<p class="quota-caption ${age>24?'warning':''}">记录于 ${fmtTime(q.timestamp)}${age>24?' · 快照已超过 24 小时':''}。读取最近会话快照，不主动查询服务器；与上方筛选范围无关，也不能据此推算逐项目额度。</p>`;
  $('quota').innerHTML=html;
}
function renderHealth() {
  const c=data.collection,s=data.schedule;const schedule=s?.installed?'每日 '+String(s.Hour).padStart(2,'0')+':'+String(s.Minute).padStart(2,'0')+' · '+s.timezone:'尚未安装每日计划';
  const info=[['最近成功采集',fmtTime(c.updated_at)],['可读会话文件',(c.files??0)+' 个'],['本次采集耗时',(c.seconds??0)+' 秒'],['本次读取数据',compact(c.bytes_read??0)+'B'],['固定计划',schedule]];
  $('health').innerHTML=info.map(([k,v])=>`<div class="info-row"><span>${k}</span><span>${escapeHtml(v)}</span></div>`).join('');
  if(c.missing_rollouts||c.inherited_baselines||c.warnings?.length){const notes=[];if(c.missing_rollouts)notes.push(c.missing_rollouts+' 个会话缺少原始文件');if(c.inherited_baselines)notes.push(c.inherited_baselines+' 个会话的首条继承累计未计入');if(c.warnings?.length)notes.push(c.warnings.length+' 条采集异常，详情见本地运行报告');$('health').innerHTML+=`<p class="quota-caption warning">覆盖提示：${escapeHtml(notes.join('；'))}。</p>`;}
}
document.querySelectorAll('[data-period]').forEach(button=>button.addEventListener('click',()=>{period=button.dataset.period;document.querySelectorAll('[data-period]').forEach(b=>b.classList.toggle('selected',b===button));render();}));
$('month').addEventListener('change',()=>{if($('month').value)render();});$('search').addEventListener('input',renderProjects);
$('refresh').addEventListener('click',async()=>{const b=$('refresh');b.disabled=true;b.textContent='正在采集…';try{const result=await api('/api/refresh',{});await load();toast('采集完成：新增 '+result.new_events+' 条用量记录，用时 '+result.seconds+' 秒。');}catch(e){toast(e.message);}finally{b.disabled=false;b.textContent='↻　立即采集';}});
$('settings').addEventListener('click',()=>{$('amount').value=data.config.subscription_monthly??'';$('currency').value=data.config.currency;$('settings-dialog').showModal();});
$('cancel').addEventListener('click',()=>$('settings-dialog').close());
$('settings-form').addEventListener('submit',async event=>{event.preventDefault();try{await api('/api/settings',{subscription_monthly:$('amount').value===''?null:Number($('amount').value),currency:$('currency').value});$('settings-dialog').close();await load();toast('月费设置已保存在本机。');}catch(e){toast(e.message);}});
$('export').addEventListener('click',()=>{const csvCell=value=>'"'+String(value).replace(/^[=+@-]/,"'$&").replaceAll('"','""')+'"';const rows=[['项目','归属依据','Token总量','缓存输入（已含于输入）','输出Token','占比','订阅分摊（估算）','币种']];visibleProjects.filter(p=>p.name.toLowerCase().includes($('search').value.trim().toLowerCase())).forEach(p=>rows.push([p.name,p.attribution,p.total,p.cached,p.output,(p.share*100).toFixed(4)+'%',p.cost===null?'':p.cost.toFixed(2),data.config.currency]));const blob=new Blob(['\ufeff'+rows.map(r=>r.map(csvCell).join(',')).join('\r\n')],{type:'text/csv;charset=utf-8'});const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='codex-usage-'+range().join('_')+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
$('expense-search').addEventListener('input',renderExpenses);$('expense-currency').addEventListener('change',renderExpenses);$('expense-history').addEventListener('change',renderExpenses);
$('expense-new').addEventListener('click',()=>{$('expense-form').reset();expenseRequest=crypto.randomUUID();$('expense-date').value=data.now.slice(0,10);$('expense-category').innerHTML=data.expense_categories.map(c=>`<option>${escapeHtml(c)}</option>`).join('');$('expense-category').value='其他';$('expense-error').textContent='';$('expense-duplicate-label').hidden=true;$('expense-dialog').showModal();});
$('expense-cancel').addEventListener('click',()=>$('expense-dialog').close());
$('expense-form').addEventListener('submit',async event=>{event.preventDefault();$('expense-save').disabled=true;try{const result=await api('/api/expenses/add',{amount:$('expense-amount').value,currency:$('expense-unit').value,payment_date:$('expense-date').value,payee:$('expense-payee').value,description:$('expense-description').value,category:$('expense-category').value,project:$('expense-project').value,kind:$('expense-kind').value,note:$('expense-note').value,request_key:expenseRequest,allow_duplicate:$('expense-allow-duplicate').checked});$('expense-dialog').close();await load();toast(result.created?'已记入实际支出。':'这笔请求已记入，没有重复新增。');}catch(e){$('expense-error').textContent=e.message;if(e.duplicate)$('expense-duplicate-label').hidden=false;}finally{$('expense-save').disabled=false;}});
$('expense-rows').addEventListener('click',event=>{const button=event.target.closest('[data-void-expense]');if(!button)return;voidExpense=(data.expenses||[]).find(e=>e.id===button.dataset.voidExpense);if(!voidExpense)return;$('expense-void-summary').textContent=voidExpense.payment_date+' · '+voidExpense.payee+' · '+expenseMoney(voidExpense.signed_minor,voidExpense.currency);$('expense-void-reason').value='';$('expense-void-dialog').showModal();});
$('expense-void-cancel').addEventListener('click',()=>$('expense-void-dialog').close());
$('expense-void-form').addEventListener('submit',async event=>{event.preventDefault();$('expense-void-save').disabled=true;try{await api('/api/expenses/void',{id:voidExpense.id,reason:$('expense-void-reason').value});$('expense-void-dialog').close();await load();toast('已作废，原记录仍保留。');}catch(e){toast(e.message);}finally{$('expense-void-save').disabled=false;}});
$('expense-export').addEventListener('click',()=>{const cell=v=>'"'+String(v??'').replace(/^[=+@-]/,"'$&").replaceAll('"','""')+'"';const rows=[['编号','日期','商家/工具','用途','分类','项目','类型','原币金额','币种','状态','备注','作废原因'],...visibleExpenses.map(e=>[e.id,e.payment_date,e.payee,e.description,e.category,e.project,e.kind,e.amount,e.currency,e.voided_at?'已作废':'有效',e.note,e.void_reason])];const url=URL.createObjectURL(new Blob(['\ufeff'+rows.map(r=>r.map(cell).join(',')).join('\r\n')],{type:'text/csv;charset=utf-8'})),a=document.createElement('a');a.href=url;a.download='ai-expenses-'+range().join('_')+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
load().catch(e=>toast('读取失败：'+e.message+'。请重新打开看板。'));
setInterval(()=>load().catch(()=>{}),60000);
