const state={virtual:null,real:null,virtualReport:null,realReport:null,health:null,quality:null,quotes:[]};
const $=id=>document.getElementById(id);
const esc=value=>String(value??"—").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const money=c=>c==null?"—":new Intl.NumberFormat("zh-CN",{style:"currency",currency:"CNY",maximumFractionDigits:2}).format(c/100);
const price=c=>c==null?"—":(c/100).toFixed(2);
const pct=n=>n==null?"—":(Number(n)*100).toFixed(2)+"%";
const ratePct=n=>n==null?"—":(Number(n)*100).toFixed(3)+"%";
const shownTime=value=>value?new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):"—";
const status=value=>({watching:"观察中",triggered:"已触发",virtual_bought:"虚拟已买",opportunity_only:"虚拟资金不足，仅记录",expired:"已失效",blocked:"已阻断",funded:"预算已预留",cash_insufficient:"虚拟资金不足",held:"已有虚拟持仓",lot_too_small:"不足一手",open:"持仓中"})[value]||value||"—";
const tag=value=>'<span class="tag">'+esc(status(value))+'</span>';

async function api(path,options={}){
  const response=await fetch(path,{headers:{"Content-Type":"application/json"},...options});
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
function alerts(rows){
  if(!rows.length)return '<div class="empty">暂无提醒</div>';
  return rows.slice(0,30).map(a=>'<div class="alert"><div class="'+(a.read_at?'':'unread')+'"><strong>'+esc(a.title)+(a.code?' · '+esc(a.code):'')+'</strong><p>'+esc(a.body)+'</p><small>'+esc(shownTime(a.created_at))+'</small></div>'+(a.read_at?'':'<button class="secondary" data-read="'+a.id+'">标记已读</button>')+'</div>').join('');
}
function renderVirtual(){
  const v=state.virtual,r=state.virtualReport;if(!v||!r)return;
  $('v-capital').textContent=money(v.capital_cents);
  $('v-available').textContent=money(v.available_budget_cents);
  $('v-cash').textContent=money(v.cash_cents);
  $('v-pnl').textContent=money(r.virtual_realized_pnl_cents);
  $('v-date').textContent=v.date+' · 候选 '+(v.watchlist[0]?.candidate_count??'—')+' 只';
  $('v-watch').innerHTML=table(['排名 / 股票','5日收益','20日收益','买入限价','单笔预算','计划股数','止盈档位','最新价','虚拟资格','虚拟状态'],v.watchlist.map(w=>[
    esc(w.rank)+' · <strong>'+esc(w.code)+'</strong>',pct(w.ret5),pct(w.ret20),price(w.buy_limit_cents),money(w.order_budget_cents??v.settings.budget_cents),esc(w.planned_shares),pct(w.target_pct),latestPrice(w.code,v.date),tag(w.eligibility),tag(w.status)
  ]));
  $('v-positions').innerHTML=table(['ID / 股票','入场日','买入价','剩余股数','首次目标','初始止损','移动线','状态'],v.positions.map(p=>[
    '<button class="link" data-virtual-trade="'+p.id+'">'+esc(p.id)+' · '+esc(p.code)+'</button>',esc(p.entry_date),price(p.entry_price_cents),esc(p.remaining_shares),pct(p.target_pct),price(p.initial_stop_cents),p.trailing_line_cents?price(p.trailing_line_cents):'未启用',tag(p.status)
  ]));
  $('v-alerts').innerHTML=alerts(v.alerts);
  $('v-report').innerHTML=table(['ID / 股票','买入日','平仓时间','净盈亏','日志'],(r.virtual_trades||[]).map(p=>[
    esc(p.id)+' · '+esc(p.code),esc(p.entry_date),esc(shownTime(p.closed_at)),money(p.closed_pnl_cents),'<button class="link" data-virtual-trade="'+p.id+'">查看</button>'
  ]));
  const s=v.settings;
  $('v-settings').innerHTML=[["本金",money(s.capital_cents)],["普通日每单",money(s.budget_cents)],["候选≥50只每单",money(20000000)],["单边佣金",ratePct(s.commission_rate)],["最小佣金",money(Math.round(Number(s.minimum_commission)*100))],["过户费",ratePct(s.transfer_rate)],["卖出印花税",ratePct(s.stamp_rate)],["滑点",ratePct(s.slippage_rate)]].map(([name,value])=>'<div class="health-row"><span>'+esc(name)+'</span><strong>'+esc(value)+'</strong></div>').join('');
  const form=$('settings-form');
  if(!form.dataset.loaded){
    for(const name of ['commission_rate','minimum_commission','transfer_rate','stamp_rate','slippage_rate'])form.elements[name].value=s[name];
    form.elements.capital.value=(s.capital_cents/100).toFixed(2);
    form.elements.budget.value=(s.budget_cents/100).toFixed(2);
    form.dataset.loaded='1';
  }
}
function renderReal(){
  const r=state.real,report=state.realReport;if(!r||!report)return;
  $('r-count').textContent=r.watchlist.length+' 只';
  $('r-open').textContent=r.positions.length+' 笔';
  $('r-pnl').textContent=money(report.actual_closed_realized_pnl_cents);
  $('r-date').textContent=r.date;
  $('r-watch').innerHTML=table(['排名 / 股票','5日收益','20日收益','买入限价','单笔计划上限','参考股数','止盈档位','最新价','实盘买点'],r.watchlist.map(w=>[
    esc(w.rank)+' · <strong>'+esc(w.code)+'</strong>',pct(w.ret5),pct(w.ret20),price(w.buy_limit_cents),money(w.order_budget_cents),esc(w.planned_shares),pct(w.target_pct),latestPrice(w.code,r.date),w.buy_signal_at?'已提醒 '+esc(shownTime(w.buy_signal_at)):'等待价格条件'
  ]));
  $('r-positions').innerHTML=table(['ID / 股票','入场日','买入价','剩余股数','首次目标','初始止损','移动线','关联规则'],r.positions.map(p=>[
    '<button class="link" data-real-position="'+p.id+'">'+esc(p.id)+' · '+esc(p.code)+'</button>',esc(p.entry_date),price(p.entry_price_cents),esc(p.remaining_shares),pct(p.target_pct),price(p.initial_stop_cents),p.trailing_line_cents?price(p.trailing_line_cents):'未启用',p.watch_id?'已关联':'未关联'
  ]));
  $('r-alerts').innerHTML=alerts(r.alerts);
  $('r-report').innerHTML=table(['ID / 股票','买入日','平仓时间','已录入净盈亏','成交'],(report.actual_trades||[]).map(p=>[
    esc(p.id)+' · '+esc(p.code),esc(p.entry_date),esc(shownTime(p.closed_at)),money(p.net_pnl_cents),'<button class="link" data-real-position="'+p.id+'">查看</button>'
  ]));
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
function render(){renderVirtual();renderReal();renderSystem()}
let toastTimer;
function toast(message){const box=$('toast');box.textContent=message;box.classList.remove('hidden');clearTimeout(toastTimer);toastTimer=setTimeout(()=>box.classList.add('hidden'),5000)}
async function refresh(){
  try{
    const [virtual,real,virtualReport,realReport,health,quality,quotes]=await Promise.all([
      api('/api/virtual'),api('/api/real'),api('/api/virtual/report'),api('/api/real/report'),api('/api/health'),api('/api/quote-quality?days=5'),api('/api/quotes')
    ]);
    Object.assign(state,{virtual,real,virtualReport,realReport,health,quality,quotes});render();
  }catch(error){toast('刷新失败：'+error.message)}
}
function show(view){document.querySelectorAll('.nav').forEach(button=>button.classList.toggle('active',button.dataset.view===view));document.querySelectorAll('.view').forEach(panel=>panel.classList.toggle('active',panel.id==='view-'+view))}
function localDateInput(){const d=new Date();return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,16)}
document.querySelectorAll('.nav').forEach(button=>button.addEventListener('click',()=>show(button.dataset.view)));
document.querySelectorAll('.refresh').forEach(button=>button.addEventListener('click',refresh));
document.addEventListener('click',async event=>{
  const target=event.target;
  if(target.dataset.read){try{await api('/api/alerts/'+target.dataset.read+'/read',{method:'POST'});await refresh()}catch(error){toast(error.message)}return}
  if(target.dataset.virtualTrade){try{const detail=await api('/api/trades/'+target.dataset.virtualTrade+'/events');$('v-trade-detail').innerHTML=table(['时间','方向','股数','代理价格','现金变动','原因'],detail.legs.map(leg=>[
    esc(shownTime(leg.at)),leg.side==='buy'?'买入':'卖出',esc(leg.shares),price(leg.gross_price_cents),money(leg.cash_delta_cents),esc(leg.reason)
  ]))}catch(error){toast(error.message)}return}
  if(target.dataset.realPosition){try{const fills=await api('/api/positions/actual/'+target.dataset.realPosition+'/fills');$('r-fill-detail').innerHTML=table(['时间','方向','股数','成交价','费用'],fills.map(fill=>[
    esc(shownTime(fill.happened_at)),fill.side==='buy'?'买入':'卖出',esc(fill.shares),price(fill.price_cents),money(fill.fee_cents)
  ]))}catch(error){toast(error.message)}}
});
for(const side of ['buy','sell']){
  const form=$('actual-'+side+'-form');form.elements.happened_at.value=localDateInput();
  form.addEventListener('submit',async event=>{
    event.preventDefault();const data=Object.fromEntries(new FormData(form));
    data.side=side;data.shares=Number(data.shares);data.price=Number(data.price);data.fee=Number(data.fee||0);
    data.happened_at=new Date(data.happened_at).toISOString();data.idempotency_key=crypto.randomUUID();
    if(side==='buy')data.watch_id=data.watch_id?Number(data.watch_id):null;
    else{data.position_id=Number(data.position_id);delete data.code;delete data.watch_id}
    try{const result=await api('/api/actual-fills',{method:'POST',body:JSON.stringify(data)});$('form-message').textContent='已记录实际'+(side==='buy'?'买入':'卖出')+'，持仓 ID：'+result.position_id;form.reset();form.elements.happened_at.value=localDateInput();await refresh()}catch(error){$('form-message').textContent=error.message}
  });
}
$('settings-form').addEventListener('submit',async event=>{
  event.preventDefault();
  try{const result=await api('/api/settings',{method:'POST',body:JSON.stringify(Object.fromEntries(new FormData(event.target)))});toast('虚拟盘参数将于 '+result.effective_date+' 生效');await refresh()}catch(error){toast(error.message)}
});
setInterval(()=>$('clock').textContent=new Date().toLocaleString('zh-CN',{hour12:false}),1000);
setInterval(refresh,10000);
try{const stream=new EventSource('/api/events/stream');stream.addEventListener('update',refresh)}catch{}
refresh();
