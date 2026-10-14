const state={virtual:null,real:null,virtualReport:null,virtualActions:null,virtualHistoryPage:1,virtualClosed:null,virtualClosedPage:1,realReport:null,realHistoryPage:1,realClosed:null,realClosedPage:1,health:null,quality:null,quotes:[]};
const $=id=>document.getElementById(id);
const esc=value=>String(value??"—").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const money=c=>c==null?"—":new Intl.NumberFormat("zh-CN",{style:"currency",currency:"CNY",maximumFractionDigits:2}).format(c/100);
const price=c=>c==null?"—":(c/100).toFixed(2);
const pct=n=>n==null?"—":(Number(n)*100).toFixed(2)+"%";
const pnlQuoteTime=value=>new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false,month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
const currentPnl=position=>position.current_pnl_pct==null?'—':'<span class="'+(position.current_pnl_pct>0?'positive':position.current_pnl_pct<0?'negative':'')+'" title="含已卖出净收入，余仓按报价估值；未扣未来卖出费用">'+esc(pct(position.current_pnl_pct))+'</span><small class="tiny pnl-asof">截至 '+esc(pnlQuoteTime(position.pnl_quote_time))+'</small>';
const shownTime=value=>value?new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}):"—";
const status=value=>({watching:"观察中",triggered:"已触发",virtual_bought:"虚拟已买",opportunity_only:"虚拟资金不足，仅记录",expired:"已失效",blocked:"已阻断",funded:"预算已预留",cash_insufficient:"虚拟资金不足",held:"已有虚拟持仓",lot_too_small:"不足一手",open:"持仓中"})[value]||value||"—";
const tag=value=>'<span class="tag">'+esc(status(value))+'</span>';

async function api(path,options={}){
  let response;
  try{response=await fetch(path,{headers:{"Content-Type":"application/json"},...options})}
  catch(error){throw Error(path+' 请求失败：'+error.message)}
  if(!response.ok){let message=await response.text();try{message=JSON.parse(message).detail||message}catch{}throw Error(message)}
  return response.json();
}
function table(headers,rows){
  if(!rows.length)return '<div class="empty">暂无记录</div>';
  return '<div class="table-scroll"><table><thead><tr>'+headers.map(h=>'<th>'+esc(h)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(row=>'<tr>'+row.map(cell=>'<td>'+cell+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>';
}
function latestPrice(code,day){
  const q=state.quotes.find(row=>row.code===code&&row.healthy&&String(row.source_time).slice(0,10)===day);
  return q?price(q.last_cents):"—";
}
function buyAllocation(watch){
  const surge=watch?.candidate_count!=null?Number(watch.candidate_count)>=50:Number(watch?.order_budget_cents)>=20000000;
  return surge?'重仓（建议20%）':'轻仓（建议7.5%）';
}
function realAlerts(rows){
  if(!rows.length)return '<div class="empty">暂无提醒</div>';
  return rows.slice(0,30).map(a=>{
    const expiredBuy=a.kind==='real_buy'&&a.event_key.split(':')[1]!==state.real.date;
    const actionable=a.action_status==='pending'&&a.reference_price_cents>0&&!expiredBuy;
    const review=a.kind!=='real_buy'&&a.kind!=='actual_sell';
    const result=a.action_status==='completed'?'已完成':a.action_status==='skipped'?'已跳过':expiredBuy?'已过期':a.action_status==='legacy'?'历史提醒':review?'需人工核对':'待处理';
    const actions=actionable?'<div class="alert-actions"><button class="primary" data-real-action="complete" data-alert-id="'+a.id+'">已完成</button><button class="secondary" data-real-action="skip" data-alert-id="'+a.id+'">跳过</button></div>':review&&!a.read_at?'<button class="secondary" data-read="'+a.id+'">已知悉</button>':'';
    const watch=a.kind==='real_buy'?state.real.watchlist.find(w=>w.code===a.code&&w.entry_date===a.event_key.split(':')[1]):null;
    const title=a.kind==='real_buy'?'买入':a.kind==='actual_sell'?'卖出':a.title;
    const body=a.kind==='real_buy'?'提醒价 '+price(a.reference_price_cents)+' 元 · '+buyAllocation(watch):a.kind==='actual_sell'?(a.action_kind==='partial_profit'?'首次止盈：卖出约半仓':'清仓：卖出剩余持仓')+' · 提醒价 '+price(a.reference_price_cents)+' 元':a.body;
    return '<div class="alert"><div class="'+(actionable?'unread':'')+'"><strong>'+esc(title)+(a.code?' · '+esc(a.code):'')+'</strong><p>'+esc(body)+'</p><small>'+esc(shownTime(a.created_at))+' · '+esc(result)+'</small></div>'+actions+'</div>';
  }).join('');
}
function renderVirtual(){
  const v=state.virtual,r=state.virtualReport;if(!v||!r)return;
  $('v-capital').textContent=money(v.capital_cents);
  $('v-available').textContent=money(v.available_budget_cents);
  $('v-cash').textContent=money(v.cash_cents);
  $('v-assets').textContent=money(v.total_asset_cents);
  const assetNote=$('v-assets-note');
  assetNote.textContent=v.valuation_fallback_codes.length?'部分持仓缺少报价，按买入价估算':'';
  assetNote.classList.toggle('hidden',!v.valuation_fallback_codes.length);
  $('v-date').textContent=v.date+' · 候选 '+(v.watchlist[0]?.candidate_count??'—')+' 只';
  $('v-watch').innerHTML=table(['排名 / 股票','买入限价','首次止盈价','单笔预算','计划股数','止盈档位','最新价','虚拟资格','虚拟状态'],v.watchlist.map(w=>[
    esc(w.rank)+' · <strong>'+esc(w.code)+'</strong>',price(w.buy_limit_cents),price(w.first_target_cents),money(w.order_budget_cents??v.settings.budget_cents),esc(w.planned_shares),pct(w.target_pct),latestPrice(w.code,v.date),tag(w.eligibility),tag(w.status)
  ]));
  $('v-positions').innerHTML=table(['ID / 股票','入场日','买入价','剩余股数','当前盈亏','首次目标','初始止损','移动线','状态'],v.positions.map(p=>[
    esc(p.id)+' · <strong>'+esc(p.code)+'</strong>',esc(p.entry_date),price(p.entry_price_cents),esc(p.remaining_shares),currentPnl(p),pct(p.target_pct),price(p.initial_stop_cents),p.trailing_line_cents?price(p.trailing_line_cents):'未启用',tag(p.status)
  ]));
}
function renderVirtualActions(history){
  $('v-actions').innerHTML=table(['时间','股票','操作','股数','价格','费用','现金变动','原因'],history.actions.map(a=>[
    esc(shownTime(a.at)),'<strong>'+esc(a.code)+'</strong>',a.side==='buy'?'买入':'卖出',esc(a.shares),price(a.gross_price_cents),money(a.fee_cents),money(a.cash_delta_cents),esc(({strict_crossing:'限价买入',partial_profit:'首次止盈',stop:'止损或退出线',expiry:'持仓到期'})[a.reason]||a.reason)
  ]));
  state.virtualHistoryPage=history.page;
  $('v-history-page').textContent='第 '+history.page+' / '+history.pages+' 页 · 共 '+history.total+' 条';
  $('v-history-prev').disabled=history.page<=1;
  $('v-history-next').disabled=history.page>=history.pages;
}
function renderRealHistory(report){
  $('r-report').innerHTML=table(['股票','操作','提醒价','处理时间','结果'],(report.actions||[]).map(a=>[
    '<strong>'+esc(a.code)+'</strong>',esc(a.action_kind==='buy'?'买入':a.action_kind==='partial_profit'?'首次止盈':a.action_kind==='manual_finish'?'自行清仓':'清仓'),price(a.reference_price_cents),esc(shownTime(a.acted_at)),a.action_status==='completed'?'已完成':'已跳过'
  ]));
  state.realHistoryPage=report.page;
  $('r-history-page').textContent='第 '+report.page+' / '+report.pages+' 页 · 共 '+report.total+' 条';
  $('r-history-prev').disabled=report.page<=1;
  $('r-history-next').disabled=report.page>=report.pages;
}
function pageStatus(prefix,history){
  $(prefix+'-page').textContent='第 '+history.page+' / '+history.pages+' 页 · 共 '+history.total+' 笔';
  document.querySelector('[data-'+(prefix==='v-positions'?'virtual':'real')+'-positions-page="prev"]').disabled=history.page<=1;
  document.querySelector('[data-'+(prefix==='v-positions'?'virtual':'real')+'-positions-page="next"]').disabled=history.page>=history.pages;
}
const legName=reason=>({strict_crossing:'限价买入',partial_profit:'首次止盈',stop:'止损或退出线',expiry:'持仓到期',buy:'买入',manual_finish:'自行清仓'})[reason]||reason||'清仓';
function renderVirtualClosed(history){
  $('v-closed-positions').innerHTML=history.positions.length?history.positions.map(p=>
    '<div class="history-position"><div class="history-facts"><strong>'+esc(p.code)+'</strong><span>买入价 '+price(p.entry_price_cents)+' 元</span><span>买入时间 '+esc(shownTime(p.buy_time))+'</span><span>持仓 '+esc(p.hold_days)+' 个交易日</span><span>净盈亏 <strong class="'+(p.closed_pnl_cents>=0?'positive':'negative')+'">'+esc(money(p.closed_pnl_cents))+' / '+esc(pct(p.net_return_pct))+'</strong></span></div>'+table(['时间','交易','股数','价格','费用','现金变动'],p.transactions.map(t=>[
      esc(shownTime(t.at)),esc(t.side==='buy'?'买入':'卖出 · '+legName(t.reason)),esc(t.shares),price(t.gross_price_cents),money(t.fee_cents),money(t.cash_delta_cents)
    ]))+'</div>').join(''):'<div class="empty">暂无历史持仓</div>';
  state.virtualClosedPage=history.page;pageStatus('v-positions',history);
}
function renderRealClosed(history){
  $('r-closed-positions').innerHTML=history.positions.length?history.positions.map(p=>
    '<div class="history-position"><div class="history-facts"><strong>'+esc(p.code)+'</strong><span>买入价 '+price(p.entry_price_cents)+' 元</span><span>买入时间 '+esc(p.buy_time?shownTime(p.buy_time):'历史记录未保存')+'</span><span>持仓 '+esc(p.hold_calendar_days)+' 个自然日</span><span>估算毛收益率 <strong class="'+(p.estimated_gross_return_pct>=0?'positive':'negative')+'">'+esc(p.estimated_gross_return_pct==null?'无法计算':pct(p.estimated_gross_return_pct))+'</strong></span></div>'+table(['提醒时间','交易','提醒价','处理时间','结果'],p.transactions.map(t=>[
      esc(shownTime(t.signal_at)),esc(legName(t.action_kind)),price(t.reference_price_cents),esc(shownTime(t.acted_at)),esc(t.action_status==='skipped'?'已跳过':'已完成')
    ]))+'</div>').join(''):'<div class="empty">暂无历史持仓</div>';
  state.realClosedPage=history.page;pageStatus('r-positions',history);
}
function renderReal(){
  const r=state.real,report=state.realReport;if(!r||!report)return;
  $('r-count').textContent=r.watchlist.length+' 只';
  $('r-open').textContent=r.positions.filter(p=>p.management_mode==='system').length+' 笔';
  $('r-manual').textContent=r.positions.filter(p=>p.management_mode==='user_managed').length+' 笔';
  $('r-date').textContent=r.date;
  const heldCodes=new Set(r.positions.map(p=>p.code));
  const buySignals=r.watchlist.filter(w=>!w.buy_signal_at&&w.watch_status!=='blocked'&&w.watch_status!=='expired'&&!heldCodes.has(w.code));
  $('r-watch').innerHTML=table(['排名 / 股票','买入限价','首次止盈价','建议仓位','止盈档位','最新价','状态'],buySignals.map(w=>[
    esc(w.rank)+' · <strong>'+esc(w.code)+'</strong>',price(w.buy_limit_cents),price(w.first_target_cents),esc(buyAllocation(w)),pct(w.target_pct),latestPrice(w.code,r.date),'等待触及限价'
  ]));
  const pendingSells=new Set(r.alerts.filter(a=>a.kind==='actual_sell'&&a.action_status==='pending').map(a=>a.real_position_id));
  $('r-sell-signals').innerHTML=table(['股票','记录买入价','首次止盈价','止损 / 退出线','最新价','监听状态'],r.positions.filter(p=>p.management_mode==='system').map(p=>[
    '<strong>'+esc(p.code)+'</strong>',price(p.entry_price_cents),p.partial_done?'已完成':price(p.first_target_cents),price(p.partial_done?Math.max(p.entry_price_cents,p.trailing_line_cents):p.initial_stop_cents),latestPrice(p.code,r.date),pendingSells.has(p.id)?'卖出提醒待处理':p.partial_done?'监听余仓退出':'监听首次止盈与初始止损'
  ]));
  $('r-positions').innerHTML=table(['股票','买入时间','记录买入价','首次目标','初始止损','移动线','阶段','状态'],r.positions.map(p=>[
    '<strong>'+esc(p.code)+'</strong>',esc(p.buy_signal_at?shownTime(p.buy_signal_at):'历史记录未保存'),price(p.entry_price_cents),pct(p.target_pct),price(p.initial_stop_cents),p.trailing_line_cents?price(p.trailing_line_cents):'未启用',p.partial_done?'余仓跟踪':'等待首次止盈',p.management_mode==='system'?'系统托管':'用户自行操作持仓 <button class="link" data-finish-manual="'+p.id+'">已自行清仓</button>'
  ]));
  $('r-alerts').innerHTML=realAlerts(r.alerts.filter(a=>(a.kind==='real_buy'&&a.action_status==='pending'&&a.event_key.split(':')[1]===r.date)||(a.kind==='actual_sell'&&a.action_status==='pending')||(a.event_key.startsWith('actual_')&&!a.read_at)));
  renderRealHistory(report);
}
function renderSystem(){
  const h=state.health,q=state.quality;if(!h||!q)return;
  $('worker-badge').textContent=h.worker_healthy?'后台运行中':'后台未连接';
  $('worker-badge').className='badge '+(h.worker_healthy?'good':'bad');
  $('mode-badge').textContent=h.observe_only?'观察模式':'买卖提醒已启用';
  $('mode-badge').className='badge '+(h.observe_only?'warn':'good');
  const quality=h.status?.daily_quality?.value;
  const quotes=h.status?.quote_health?.value;
  const problems=[];
  if(!h.worker_healthy)problems.push('后台未连接');
  if(quality&&quality.ok===false)problems.push('日线质量未通过，暂停新买入');
  if(quotes&&quotes.healthy===false)problems.push('部分报价缺失，相关股票暂不触发提醒');
  $('critical-banner').textContent=problems.join('；');
  $('critical-banner').classList.toggle('hidden',!problems.length);
  $('system-health').innerHTML=[["最近日线",quality?.latest||'尚未检查'],["日线质量",quality?(quality.ok?'通过':'未通过'):'尚未检查'],["行情来源",quotes?.provider||'—'],["缺失代码",(quotes?.missing_codes||[]).join('、')||'无'],["后台心跳",h.worker_age_seconds==null?'—':Math.round(h.worker_age_seconds)+' 秒前']].map(([name,value])=>'<div class="health-row"><span>'+esc(name)+'</span><strong>'+esc(value)+'</strong></div>').join('');
  $('system-quality').innerHTML=table(['日期','来源','轮询','请求股次','收到股次'],(q.polls||[]).map(row=>[
    esc(row.day),esc(row.provider),esc(row.polls),esc(row.requested_quotes),esc(row.received_quotes)
  ]));
}
function render(){renderVirtual();if(state.virtualActions)renderVirtualActions(state.virtualActions);if(state.virtualClosed)renderVirtualClosed(state.virtualClosed);renderReal();if(state.realClosed)renderRealClosed(state.realClosed);renderSystem()}
let toastTimer;
function toast(message){const box=$('toast');box.textContent=message;box.classList.remove('hidden');clearTimeout(toastTimer);toastTimer=setTimeout(()=>box.classList.add('hidden'),5000)}
let refreshPromise=null;
function refresh(){
  if(refreshPromise)return refreshPromise;
  refreshPromise=(async()=>{
    try{
      const [virtual,real,virtualReport,virtualActions,virtualClosed,realReport,realClosed,health,quality,quotes]=await Promise.all([
        api('/api/virtual'),api('/api/real'),api('/api/virtual/report'),api('/api/virtual/actions?page='+state.virtualHistoryPage),api('/api/virtual/positions/history?page='+state.virtualClosedPage),api('/api/real/report?page='+state.realHistoryPage),api('/api/real/positions/history?page='+state.realClosedPage),api('/api/health'),api('/api/quote-quality?days=5'),api('/api/quotes')
      ]);
      Object.assign(state,{virtual,real,virtualReport,virtualActions,virtualClosed,realReport,realClosed,health,quality,quotes});render();
    }catch(error){toast('刷新失败：'+error.message)}
  })().finally(()=>{refreshPromise=null});
  return refreshPromise;
}
async function refreshAfterWrite(){if(refreshPromise)await refreshPromise;await refresh()}
function show(view){document.querySelectorAll('.nav').forEach(button=>button.classList.toggle('active',button.dataset.view===view));document.querySelectorAll('.view').forEach(panel=>panel.classList.toggle('active',panel.id==='view-'+view))}
function showRealTab(tab){
  document.querySelectorAll('[data-real-tab]').forEach(button=>{const active=button.dataset.realTab===tab;button.classList.toggle('active',active);button.setAttribute('aria-selected',String(active))});
  document.querySelectorAll('#view-real .real-tab-panel').forEach(panel=>panel.classList.toggle('active',panel.id==='real-panel-'+tab));
}
function showVirtualTab(tab){
  document.querySelectorAll('[data-virtual-tab]').forEach(button=>{const active=button.dataset.virtualTab===tab;button.classList.toggle('active',active);button.setAttribute('aria-selected',String(active))});
  document.querySelectorAll('#view-virtual .real-tab-panel').forEach(panel=>panel.classList.toggle('active',panel.id==='virtual-panel-'+tab));
}
document.querySelectorAll('.nav').forEach(button=>button.addEventListener('click',()=>show(button.dataset.view)));
document.querySelectorAll('[data-real-tab]').forEach(button=>button.addEventListener('click',()=>showRealTab(button.dataset.realTab)));
document.querySelectorAll('[data-virtual-tab]').forEach(button=>button.addEventListener('click',()=>showVirtualTab(button.dataset.virtualTab)));
document.querySelectorAll('.refresh').forEach(button=>button.addEventListener('click',refresh));
document.addEventListener('click',async event=>{
  const target=event.target;
  if(target.dataset.virtualPositionsPage){
    target.disabled=true;
    try{
      if(refreshPromise)await refreshPromise;
      const next=state.virtualClosedPage+(target.dataset.virtualPositionsPage==='next'?1:-1);
      const history=await api('/api/virtual/positions/history?page='+next);
      state.virtualClosed=history;renderVirtualClosed(history);
    }catch(error){toast(error.message);target.disabled=false}
    return;
  }
  if(target.dataset.realPositionsPage){
    target.disabled=true;
    try{
      if(refreshPromise)await refreshPromise;
      const next=state.realClosedPage+(target.dataset.realPositionsPage==='next'?1:-1);
      const history=await api('/api/real/positions/history?page='+next);
      state.realClosed=history;renderRealClosed(history);
    }catch(error){toast(error.message);target.disabled=false}
    return;
  }
  if(target.dataset.virtualHistoryPage){
    target.disabled=true;
    try{
      if(refreshPromise)await refreshPromise;
      const next=state.virtualHistoryPage+(target.dataset.virtualHistoryPage==='next'?1:-1);
      const history=await api('/api/virtual/actions?page='+next);
      state.virtualActions=history;renderVirtualActions(history);
    }catch(error){toast(error.message);target.disabled=false}
    return;
  }
  if(target.dataset.historyPage){
    target.disabled=true;
    try{
      if(refreshPromise)await refreshPromise;
      const next=state.realHistoryPage+(target.dataset.historyPage==='next'?1:-1);
      const report=await api('/api/real/report?page='+next);
      state.realReport=report;renderRealHistory(report);
    }catch(error){toast(error.message);target.disabled=false}
    return;
  }
  if(target.dataset.realAction){
    target.disabled=true;
    try{await api('/api/real/alerts/'+target.dataset.alertId+'/'+target.dataset.realAction,{method:'POST'});await refreshAfterWrite()}
    catch(error){toast(error.message);target.disabled=false}
    return;
  }
  if(target.dataset.finishManual){
    target.disabled=true;
    try{await api('/api/real/positions/'+target.dataset.finishManual+'/finish-manual',{method:'POST'});await refreshAfterWrite()}
    catch(error){toast(error.message);target.disabled=false}
    return;
  }
  if(target.dataset.read){try{await api('/api/alerts/'+target.dataset.read+'/read',{method:'POST'});await refresh()}catch(error){toast(error.message)}return}
});
function updateClock(){$('clock').textContent=new Date().toLocaleString('zh-CN',{hour12:false})}
updateClock();
setInterval(updateClock,1000);
setInterval(refresh,10000);
try{const stream=new EventSource('/api/events/stream');stream.addEventListener('update',refresh)}catch{}
refresh();
