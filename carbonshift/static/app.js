'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number = (value, digits=3) => value == null ? '—' : Number(value).toLocaleString('en-IN', {maximumFractionDigits:digits});
const dateOf = value => new Date(typeof value === 'number' ? value * 1000 : value);
const time = value => value == null ? '—' : dateOf(value).toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata',hour:'2-digit',minute:'2-digit',hour12:false});
const stamp = value => value == null ? '—' : dateOf(value).toLocaleString('en-IN',{timeZone:'Asia/Kolkata',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}) + ' IST';
let plan = null, selectedJob = null, currentJob = null, system = null, pendingPayload = null;
let submitting = false, toastTimer, polling = false, planGeneration = 0;
let executionPlan=null, attempted=false, runGeneration=0, previewing=false;
function notify(message) { $('toast').textContent=message; $('toast').hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>{$('toast').hidden=true;},10000); }
async function api(path, options={}) {
  const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),15000);
  try {
    const response=await fetch(path,{...options,headers:{'Content-Type':'application/json',...(options.headers||{})},signal:controller.signal});
    const data=await response.json();
    if(!response.ok) {
      const detail=Array.isArray(data.detail) ? data.detail.map(x=>`${x.loc?.slice(1).join('.')}: ${x.msg}`).join('\n') : data.detail;
      throw new Error(detail || `Request failed (${response.status})`);
    }
    return data;
  } catch(error) { if(error.name==='AbortError') throw new Error('The local API did not respond within 15 seconds. Check the app terminal.'); throw error; }
  finally { clearTimeout(timer); }
}
function download(value,name) {
  const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));
  const a=document.createElement('a'); a.href=url; a.download=name; a.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
}
function fields(form) { return Object.fromEntries(new FormData(form)); }
async function updatePlan(event) {
  event?.preventDefault(); const generation=++planGeneration;
  const data=fields($('plan-form'));
  for(const key of ['duration_minutes','estimated_power_w','base_load_kw','pv_capacity_kwp']) data[key]=Number(data[key]);
  if($('workload-choice').value==='images') { await previewImages(data,generation); return; }
  $('optimize-button').disabled=true; $('optimize-button').textContent='Comparing windows…';
  try {
    const result=await api('/api/planner',{method:'POST',body:JSON.stringify(data)});
    if(generation!==planGeneration) return;
    plan=result; renderPlan(); $('plan-output').classList.remove('stale'); $('export-plan').disabled=false;
  } catch(error) { notify(error.message); }
  finally { $('optimize-button').disabled=false; $('optimize-button').innerHTML='Find best window <span>→</span>'; }
}
function renderPlan() {
  const {request:q,result:r}=plan, b=r.baseline, n=r.recommended;
  const carbon=q.objective!=='solar';
  $('reduction-label').textContent=carbon?'Estimated emissions reduction':'Estimated grid reduction';
  const percent=carbon ? (b.estimated_grid_emissions_gco2e>0 ? 100*r.estimated_grid_emissions_reduction_gco2e/b.estimated_grid_emissions_gco2e : 0) : r.estimated_grid_energy_reduction_percent;
  $('reduction').textContent=number(percent,1)+'%';
  $('reduction-sub').textContent=carbon ? `${number(r.estimated_grid_emissions_reduction_gco2e,2)} gCO₂e modeled reduction` : `${number(r.estimated_grid_energy_reduction_kwh)} kWh less grid energy`;
  $('solar-share').textContent=number(n.estimated_solar_fraction*100,1)+'%';
  $('total-energy').textContent=number(n.estimated_total_energy_kwh)+' kWh';
  $('baseline-time').textContent=`${time(b.start)} – ${time(b.finish)} IST`;
  $('recommended-time').textContent=`${time(n.start)} – ${time(n.finish)} IST`;
  $('baseline-energy').textContent=`Grid ${number(b.estimated_grid_energy_kwh)} kWh · Solar ${number(b.estimated_solar_energy_kwh)} kWh`;
  $('recommended-energy').textContent=`Grid ${number(n.estimated_grid_energy_kwh)} kWh · Solar ${number(n.estimated_solar_energy_kwh)} kWh`;
  $('baseline-carbon').textContent=carbon ? `${number(b.estimated_grid_emissions_gco2e,2)} gCO₂e estimated` : '';
  $('recommended-carbon').textContent=carbon ? `${number(n.estimated_grid_emissions_gco2e,2)} gCO₂e estimated` : '';
  const delayed=new Date(n.start)-new Date(b.start);
  $('explanation').textContent=(delayed>0 ? `Shift by ${number(delayed/60000,0)} minutes. ` : 'The earliest start is already optimal. ') + `${r.candidate_count} candidate windows compared; the entire ${q.job.duration_minutes}-minute job finishes before its deadline.`;
  $('provenance').textContent=q.forecast.note + (carbon ? ' Carbon intensity is also invented, not a forecast for the Indian grid.' : '') + (q.objective==='carbon' ? ' Grid-only mode ignores solar.' : '') + ' No measured savings claimed.';
  $('forecast-date').textContent=dateOf(b.start).toLocaleDateString('en-IN',{timeZone:'Asia/Kolkata',weekday:'short',day:'numeric',month:'short'})+' · Mumbai time (IST)';
  $('carbon-legend').hidden=!carbon;
  renderChart(q,r);
}
function renderChart(q,r) {
  const w=Math.max(320,Math.min(900,$('forecast-chart').clientWidth)),h=238,left=45,right=48,top=20,bottom=38,plotW=w-left-right,plotH=h-top-bottom;
  const points=q.forecast.intervals,start=+new Date(q.job.earliest_start),end=+new Date(q.job.deadline);
  const max=Math.max(.01,q.site.base_load_kw,...points.map(p=>p.estimated_solar_kw))*1.2;
  const x=t=>left+(+new Date(t)-start)/(end-start)*plotW, y=v=>top+plotH-v/max*plotH;
  let svg=`<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Synthetic solar forecast with baseline and recommended job windows"><defs><linearGradient id="solar-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#72d9c2" stop-opacity=".25"/><stop offset="100%" stop-color="#72d9c2" stop-opacity="0"/></linearGradient></defs>`;
  for(let i=0;i<=4;i++){const v=max*i/4,yy=y(v);svg+=`<line x1="${left}" y1="${yy}" x2="${w-right}" y2="${yy}" stroke="#23313d" stroke-dasharray="3 5"/><text x="${left-8}" y="${yy+4}" fill="#8295a4" font-size="10" text-anchor="end">${v.toFixed(2)}</text>`;}
  svg+=`<text x="${left}" y="10" fill="#8295a4" font-size="9">kW</text>`;
  for(const [window,color] of [[r.baseline,'#e4bb79'],[r.recommended,'#b8ee96']]) svg+=`<rect x="${x(window.start)}" y="${top}" width="${x(window.finish)-x(window.start)}" height="${plotH}" fill="${color}" fill-opacity=".075" stroke="${color}" stroke-opacity=".55" stroke-dasharray="3 4"/>`;
  let path=''; points.forEach((p,i)=>{path+=`${i?'L':'M'}${x(p.start)},${y(p.estimated_solar_kw)} L${x(p.end)},${y(p.estimated_solar_kw)} `;});
  svg+=`<path d="${path} L${x(points.at(-1).end)},${y(0)} L${x(points[0].start)},${y(0)} Z" fill="url(#solar-fill)"/><path d="${path}" stroke="#72d9c2" stroke-width="2.2" fill="none"/>`;
  svg+=`<line x1="${left}" y1="${y(q.site.base_load_kw)}" x2="${w-right}" y2="${y(q.site.base_load_kw)}" stroke="#a4b2bd" stroke-dasharray="5 5"/>`;
  if(q.carbon_forecast){
    const items=q.carbon_forecast.intervals.filter(p=>+new Date(p.end)>start&&+new Date(p.start)<end), cmax=Math.max(1,...items.map(p=>p.intensity_gco2e_per_kwh))*1.15;
    let cp='';items.forEach((p,i)=>{const cy=top+plotH*(1-p.intensity_gco2e_per_kwh/cmax);cp+=`${i?'L':'M'}${x(Math.max(start,+new Date(p.start)))},${cy} L${x(Math.min(end,+new Date(p.end)))},${cy} `;});
    svg+=`<path d="${cp}" fill="none" stroke="#b49be1" stroke-width="1.5" stroke-dasharray="5 3"/><text x="${w-right+7}" y="10" fill="#b49be1" font-size="9">g/kWh</text>`;
    for(let i=0;i<=2;i++) svg+=`<text x="${w-right+7}" y="${top+plotH*(1-i/2)+4}" fill="#b49be1" font-size="10">${Math.round(cmax*i/2)}</text>`;
  }
  const ticks=w<500?3:6;
  for(let i=0;i<=ticks;i++){const t=start+(end-start)*i/ticks;svg+=`<text x="${x(t)}" y="${h-13}" fill="#8295a4" font-size="10" text-anchor="middle">${time(t/1000)}</text>`;}
  $('forecast-chart').innerHTML=svg+'</svg>';
}
function recovery(error,state) {
  const text=String(error||'').toLowerCase();
  if(text.includes('compression')) return 'This container has the old logging configuration. Let its window expire, then queue a fresh job using v0.3.';
  if(text.includes('permission denied')) return 'Your user cannot access Docker. Run docker info in the same terminal and resolve its socket permissions.';
  if(text.includes('no such image')||text.includes('image not found')) return 'Build the workload image from the project folder: docker build -t carbonshift-workload:0.4 ./workloads';
  if(text.includes('no such file')||text.includes('cannot connect')||text.includes('unavailable')) return 'Check that Docker is installed and its daemon is running: docker info. The worker checks again automatically.';
  if(state==='MISSED_WINDOW') return 'The start window expired before execution. Queue a new job after the worker is ready.';
  if(state==='RECOVERY_REQUIRED'||state==='LOST') return 'Inspect the recorded container identity before making changes. This job will not be automatically rerun.';
  if(state==='TIMED_OUT') return 'The workload exceeded its execution budget. Inspect the logs before submitting another run.';
  return 'Inspect the error and app terminal before queuing another job.';
}
function renderSystem(s) {
  system=s; const worker=s.worker,ready=s.execution_ready;
  $('connection').textContent='Local API connected'; $('connection').style.color='var(--cyan)';
  $('worker-dot').classList.toggle('ready',ready);
  $('worker-label').textContent=ready?'Worker ready · Docker image available':worker.online?'Worker online · Docker needs attention':'Worker offline or heartbeat stale';
  $('worker-detail').textContent=worker.at?`Last heartbeat ${stamp(worker.at)}${worker.age_seconds>=45?' · stale':''}`:'Start with: python -m carbonshift app';
  const banner=$('service-banner'); banner.classList.toggle('ready',ready);
  const active=(s.counts.STARTING||0)+(s.counts.RUNNING||0)+(s.counts.RECOVERY_REQUIRED||0);
  if(ready) banner.textContent=active ? 'Worker connected. An active or unresolved job currently holds the compute slot.' : 'Local worker ready. Plan with simulated forecasts, then verify execution with a real Docker workload.';
  else if(worker.online) banner.textContent='Planning is available. Execution is blocked. '+recovery(worker.error)+'\n'+(worker.error||'');
  else banner.textContent='Planning is available. Execution needs a worker. Start this workspace with: python -m carbonshift app';
  syncRunControls();syncImageControls();
  $('job-count').textContent=Object.values(s.counts).reduce((a,b)=>a+b,0);
  $('job-summary').textContent=`${s.counts.SCHEDULED||0} scheduled · ${s.counts.RUNNING||0} running · ${s.counts.SUCCEEDED||0} succeeded · ${Object.entries(s.counts).filter(([k])=>['FAILED','TIMED_OUT','MISSED_WINDOW','MISSED_DEADLINE','LOST','RECOVERY_REQUIRED'].includes(k)).reduce((a,[,v])=>a+v,0)} need review`;
}
function syncRunControls() {
  const active=system ? (system.counts.STARTING||0)+(system.counts.RUNNING||0)+(system.counts.RECOVERY_REQUIRED||0) : 0;
  const expired=executionPlan&&new Date(executionPlan.result.recommended.start).getTime()<Date.now()+2000;
  $('run-button').disabled=submitting||!system?.execution_ready||!pendingPayload||(!attempted&&(active>0||expired));
  $('preview-run').disabled=submitting||previewing;
  const countdown=$('execution-countdown');
  if(countdown&&pendingPayload) countdown.textContent=expired&&!attempted?'This start window is too close or expired. Preview a fresh plan.':`Recommended start in ${Math.max(0,Math.ceil((new Date(executionPlan.result.recommended.start)-Date.now())/1000))} seconds.`;
}
async function previewExecution() {
  if(!$('run-form').reportValidity()||previewing||submitting)return;
  const generation=++runGeneration,settings=fields($('run-form'));
  for(const key of ['run_seconds','delay_seconds','estimated_power_w'])settings[key]=Number(settings[key]);
  previewing=true;pendingPayload=null;attempted=false;syncRunControls();
  try{
    const data=await api('/api/execution/preview',{method:'POST',body:JSON.stringify(settings)});
    if(generation!==runGeneration)return;
    executionPlan=data;pendingPayload=data.submission;const r=data.result,q=data.submission;
    const shift=(new Date(r.recommended.start)-new Date(r.baseline.start))/1000;
    $('execution-preview').innerHTML=`<span class="pill">SIMULATED ENERGY · REAL WORKLOAD</span><dl class="detail-grid"><div><dt>Earliest start</dt><dd>${esc(stamp(r.baseline.start))}</dd></div><div><dt>Recommended start</dt><dd>${esc(stamp(r.recommended.start))}</dd></div><div><dt>Planned shift</dt><dd>${number(shift,0)} seconds</dd></div><div><dt>Execution / reserved slot</dt><dd>${q.execution.run_seconds} s / ${q.optimization.job.duration_minutes} min</dd></div></dl><p>${number(r.estimated_grid_energy_reduction_percent,1)}% less modeled grid energy over the reservation (${number(r.estimated_grid_energy_reduction_kwh,6)} kWh).</p><p class="help">Reservation includes startup and timeout margin. This prediction is not measured savings from the shorter actual run.</p><p id="execution-countdown" class="countdown"></p>`;
    $('run-message').textContent='Plan ready. Review the start time, then queue it.';
  }catch(e){notify(e.message);$('run-message').textContent=e.message;}
  finally{previewing=false;syncRunControls();}
}
function renderJobs(jobs) {
  $('empty-state').hidden=jobs.length>0;
  $('jobs-body').innerHTML=jobs.map(j=>{
    const runtime=j.started_at!=null?(j.finished_at??Date.now()/1000)-j.started_at:null;
    return `<tr><td>${esc(j.name)}<small>${esc(j.id.slice(0,12))}</small></td><td><span class="badge ${esc(j.state)}">${esc(j.state.replaceAll('_',' '))}</span></td><td>${esc(stamp(j.scheduled_start))}</td><td>${runtime==null?'—':number(Math.max(0,runtime),1)+' s'}</td><td>${j.exit_code??'—'}</td><td><button class="text-button job-open" data-id="${esc(j.id)}">View ↗</button></td></tr>`;
  }).join('');
}
async function refresh() {
  if(polling) return; polling=true;
  try {
    const [s,jobs]=await Promise.all([api('/api/system'),api('/api/jobs')]);
    renderSystem(s);renderJobs(jobs);$('refresh-label').textContent='Updated '+time(Date.now()/1000)+' IST';
    if(selectedJob&&$('job-dialog').open) await loadJob(selectedJob);
  } catch(error) {
    system=null;
    $('connection').textContent='API disconnected';$('connection').style.color='var(--red)';$('run-button').disabled=true;
    $('service-banner').classList.remove('ready');$('service-banner').textContent='Live status unavailable. Displayed records may be stale. '+error.message;
  } finally { polling=false; }
}
async function poll() {await refresh();setTimeout(poll,2000);}
async function loadJob(id) {
  const j=await api('/api/jobs/'+encodeURIComponent(id));
  if(selectedJob!==id) return; currentJob=j;
  $('detail-name').textContent=j.snapshot.optimization.job.name;
  const evidence=j.evidence, now=Date.now()/1000;
  const timing=j.state==='SCHEDULED' ? `Starts in ${Math.max(0,Math.ceil(j.scheduled_start-now))} seconds` : 'State reflects the last worker observation';
  $('detail-content').innerHTML=`<span class="badge ${esc(j.state)}">${esc(j.state.replaceAll('_',' '))}</span> <span class="muted">${esc(timing)}</span>
    ${j.error?`<div class="notice" style="margin-top:16px">${esc(recovery(j.error,j.state))}<br><br>${esc(j.error)}</div>`:''}
    <dl class="detail-grid"><div><dt>Scheduled start</dt><dd>${esc(stamp(j.scheduled_start))}</dd></div><div><dt>Actual start</dt><dd>${esc(stamp(j.started_at))}</dd></div><div><dt>Actual finish</dt><dd>${esc(stamp(j.finished_at))}</dd></div><div><dt>Exit code</dt><dd>${j.exit_code??'—'}</dd></div><div><dt>Recorded container runtime</dt><dd>${evidence?.runtime_seconds!=null?number(evidence.runtime_seconds,2)+' seconds':'Not recorded yet'}</dd></div><div><dt>Runtime-based energy estimate</dt><dd>${evidence?.estimated_total_energy_kwh!=null?number(evidence.estimated_total_energy_kwh,7)+' kWh':'Not recorded yet'}</dd></div></dl>
    <p class="help">Energy uses the configured power estimate × recorded runtime. No power meter or measured carbon savings. The reserved scheduling duration can exceed the workload runtime.</p>
    <div class="detail-section"><h3>Frozen scheduling prediction</h3><dl class="detail-grid"><div><dt>Earliest permitted start</dt><dd>${esc(stamp(j.recommendation.baseline.start))}</dd></div><div><dt>Recommended start</dt><dd>${esc(stamp(j.recommendation.recommended.start))}</dd></div><div><dt>Baseline grid energy · full reservation</dt><dd>${number(j.recommendation.baseline.estimated_grid_energy_kwh,6)} kWh</dd></div><div><dt>Recommended grid energy · full reservation</dt><dd>${number(j.recommendation.recommended.estimated_grid_energy_kwh,6)} kWh</dd></div></dl><p class="help">${esc(j.recommendation.forecast_note)}</p></div>
    <div class="detail-section"><h3>Container</h3><pre>${esc(j.container_name)}\n${esc(j.container_id||'Not created yet')}</pre></div>
    <div class="detail-section"><h3>Timeline</h3><ol class="events">${(j.events||[]).map(e=>`<li><time>${esc(stamp(e.at))} · ${esc(e.state)}</time>${esc(e.message)}</li>`).join('')}</ol></div>
    <div class="detail-section"><h3>Workload logs</h3><pre>${esc(j.logs||'Logs are collected when the container finishes.')}</pre></div>`;
  if(j.snapshot.execution.workload==='image-batch') {
    const progress=j.progress||{processed:0,total:null}, artifact=evidence?.artifact;
    const status=j.state==='SCHEDULED'?'Waiting for scheduled start':j.state==='STARTING'?'Starting container':j.state==='RUNNING'?'Processing images':j.state;
    const report=artifact?.report;
    const panel=document.createElement('div');panel.className='detail-section';
    panel.innerHTML=`<h3>Image batch results</h3><p>${esc(status)} · ${progress.processed}/${progress.total??'—'} images</p>
      ${progress.total?`<progress max="${progress.total}" value="${progress.processed}" aria-label="Images processed"></progress>`:''}
      ${report?`<p>${report.processed} images · Input ${number(report.input_bytes/1024,1)} KiB → Output ${number(report.output_bytes/1024,1)} KiB</p><p class="help">Processing: ${number(report.processing_seconds,2)} s · Reserved: ${j.snapshot.optimization.job.duration_minutes} min. Output size can increase.</p>
      <a class="button primary" href="/api/jobs/${encodeURIComponent(j.id)}/results">Download processed images ↓</a>
      <div class="table-scroll"><table><thead><tr><th>Original</th><th>Output</th><th>Dimensions</th><th>Size</th></tr></thead><tbody>${report.files.map(f=>`<tr><td>${esc(f.original_name)}</td><td>${esc(f.output_name)}</td><td>${f.width} × ${f.height}</td><td>${number(f.output_bytes/1024,1)} KiB</td></tr>`).join('')}</tbody></table></div>`:'<p class="help">A download becomes available after the worker verifies successful completion and the result archive.</p>'}`;
    $('detail-content').prepend(panel);
  }
  $('cancel-job').hidden=j.state!=='SCHEDULED';
}
async function openJob(id) { selectedJob=id;try{await loadJob(id);$('job-dialog').showModal();}catch(e){notify(e.message);} }
$('plan-form').addEventListener('submit',updatePlan);
$('plan-form').addEventListener('input',()=>{invalidateImages();planGeneration++;$('plan-output').classList.add('stale');$('export-plan').disabled=true;});
$('export-plan').onclick=()=>plan&&download(plan,'carbonshift-plan.json');
$('preview-run').onclick=previewExecution;
$('run-form').addEventListener('input',()=>{runGeneration++;pendingPayload=null;executionPlan=null;attempted=false;$('run-message').textContent='';$('execution-preview').innerHTML='<p>Inputs changed. Preview a fresh execution plan.</p>';syncRunControls();});
$('run-form').addEventListener('submit',async event=>{
  event.preventDefault();if(submitting||$('run-button').disabled)return;submitting=true;$('run-button').disabled=true;$('run-message').textContent='Submitting…';
  try {
    // Retain the exact payload after a network failure so retries cannot
    // silently create a duplicate workload with a new idempotency key.
    if(!pendingPayload) throw new Error('Preview an execution plan first.');
    attempted=true;
    const job=await api('/api/jobs',{method:'POST',body:JSON.stringify(pendingPayload)});
    pendingPayload=null;$('run-message').textContent=`Queued ${job.id.slice(0,12)} · ${stamp(job.scheduled_start)}`;
    if($('execution-countdown'))$('execution-countdown').textContent='Plan submitted. Follow its execution record in Job activity.';
    await refresh();await openJob(job.id);
  } catch(e) { $('run-message').textContent=e.message+' If retrying this submission, leave the fields unchanged.';notify(e.message); }
  finally {submitting=false;syncRunControls();}
});
$('jobs-body').addEventListener('click',e=>{const b=e.target.closest('.job-open');if(b)openJob(b.dataset.id);});
$('refresh').onclick=refresh;
$('close-dialog').onclick=()=>$('job-dialog').close();
$('job-dialog').addEventListener('close',()=>{selectedJob=null;currentJob=null;});
$('export-job').onclick=()=>currentJob&&download(currentJob,'carbonshift-job-'+currentJob.id+'.json');
$('cancel-job').onclick=async()=>{const id=selectedJob;if(!id)return;$('cancel-job').disabled=true;try{await api('/api/jobs/'+encodeURIComponent(id)+'/cancel',{method:'POST'});await loadJob(id);await refresh();}catch(e){notify(e.message);}finally{$('cancel-job').disabled=false;}};
document.querySelectorAll('.nav-item').forEach(a=>a.addEventListener('click',()=>{document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));a.classList.add('active');}));
$('clock').textContent=stamp(Date.now()/1000);
setInterval(()=>{$('clock').textContent=stamp(Date.now()/1000);syncRunControls();},1000);
// Initial requests run after image-batch state is initialized below.
new ResizeObserver(()=>{if(plan)renderChart(plan.request,plan.result);}).observe($('forecast-chart'));

let observations=null, carbonBusy=false;
function renderObservations(data){
  for(const r of data.records){r.age_seconds=Math.max(0,(Date.now()-new Date(r.data_at))/1000);r.stale=r.age_seconds>r.stale_after_seconds;}
  observations=data;
  $('carbon-key-status').textContent=data.configured?'Server key configured. Access to this zone depends on your Electricity Maps account.':'Set ELECTRICITYMAPS_API_KEY or run bash configure-api.sh, then restart the app. Saved observations remain readable without a key.';
  $('carbon-fetch').disabled=carbonBusy||!data.configured;
  $('carbon-export').disabled=!data.records.length;
  const latest=data.records[0];
  if(latest){
    $('carbon-latest').innerHTML=`<strong>${number(latest.carbon_intensity_gco2e_per_kwh,1)} <small>gCO₂e/kWh</small></strong><span class="pill ${latest.stale?'':'neutral'}">${latest.stale?'STALE SAVED READING':'SAVED PROVIDER READING'}</span><p>${esc(latest.zone)} · Provider time ${esc(stamp(latest.data_at))}</p><p>${latest.is_estimated?'Provider-estimated':'Not flagged as estimated by provider'}${latest.estimation_method?' · '+esc(latest.estimation_method):''} · Lifecycle, consumption-based · Hourly</p><p class="help">Retrieved ${esc(stamp(latest.retrieved_at))}. This is a grid-level reading, not a measurement of this machine or its job emissions.</p>`;
  }else $('carbon-latest').innerHTML='<p>No readings saved yet. A successful fetch adds a record here; failures never substitute simulated data.</p>';
  $('carbon-history').innerHTML=data.records.map(r=>`<tr><td>${esc(r.zone)}<small>${esc(stamp(r.data_at))}</small></td><td>${number(r.carbon_intensity_gco2e_per_kwh,1)}</td><td>${r.is_estimated?'Yes':'No'}</td><td>${r.stale?'Stale':'Within 3 h'}</td><td>${esc(stamp(r.retrieved_at))}</td></tr>`).join('');
  if(data.warnings.length)$('carbon-message').textContent=data.warnings.join('\n');
}
async function loadObservations(){
  try{renderObservations(await api('/api/carbon/observations'));}
  catch(e){$('carbon-key-status').textContent=e.message;$('carbon-fetch').disabled=true;}
}
$('carbon-form').addEventListener('submit',async event=>{
  event.preventDefault();if(carbonBusy||$('carbon-fetch').disabled)return;
  carbonBusy=true;$('carbon-fetch').disabled=true;$('carbon-message').textContent='Fetching from Electricity Maps…';
  try{
    const result=await api('/api/carbon/observe?zone='+encodeURIComponent($('carbon-zone').value),{method:'POST'});
    $('carbon-message').textContent=`Saved reading for ${result.reading.zone}. ${result.reading.stale?'Provider data is stale under the 3-hour display policy. ':''}No schedule was changed.`;
    await loadObservations();
  }catch(e){$('carbon-message').textContent=e.message+' Any reading displayed below is a previous saved observation.';}
  finally{carbonBusy=false;$('carbon-fetch').disabled=!observations?.configured;}
});
$('carbon-export').onclick=()=>observations&&download(observations,'carbonshift-observations.json');
loadObservations();
setInterval(()=>{if(observations)renderObservations(observations);},30000);

let imagePreview=null,imageAttempted=false,imageBusy=false,uploadedBatch=null;
function imageDates(){
  const local=value=>{const d=new Date(value);d.setMinutes(d.getMinutes()-d.getTimezoneOffset());return d.toISOString().slice(0,19);};
  $('image-start').value=local(Date.now()+120000);$('image-deadline').value=local(Date.now()+600000);
}
function invalidateImages(){
  imagePreview=null;imageAttempted=false;$('queue-images').disabled=true;
  $('image-status').textContent='Inputs changed. Preview the batch again before queuing.';
}
function syncImageControls(){
  const active=system?['STARTING','RUNNING','RECOVERY_REQUIRED'].reduce((n,k)=>n+(system.counts[k]||0),0):0;
  const expired=imagePreview&&new Date(imagePreview.result.recommended.start).getTime()<Date.now()+2000;
  $('queue-images').disabled=imageBusy||!imagePreview||!system?.execution_ready||(!imageAttempted&&(expired||active>0));
  if(imagePreview&&!imageBusy&&!imageAttempted) $('image-status').textContent=expired?'Start time expired. Set a future window and preview again.':!system?.execution_ready?'Plan saved. Docker worker must be ready before queuing.':`Selected start: ${stamp(imagePreview.result.recommended.start)}. Ready to queue.`;
}
$('workload-choice').addEventListener('change',()=>{
  const enabled=$('workload-choice').value==='images';$('image-inputs').hidden=!enabled;$('image-review').hidden=!enabled;
  const f=$('plan-form');f.elements.duration_minutes.max=enabled?60:540;f.elements.duration_minutes.value=enabled?2:90;
  f.elements.name.value=enabled?'image-batch':'afternoon-batch';
  $('window-help').textContent=enabled?'Choose your window above. Chart and job timestamps display Mumbai time (IST). Forecasts remain simulated.':'Planning window: tomorrow, 09:00–18:00 Mumbai time. Power and solar values are estimates.';
  if(enabled)imageDates();invalidateImages();
});
$('image-files').addEventListener('change',()=>{uploadedBatch=null;invalidateImages();});
$('image-soon').onclick=()=>{imageDates();invalidateImages();planGeneration++;$('plan-output').classList.add('stale');$('export-plan').disabled=true;};
async function fileContent(file){
  return new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result.split(',')[1]);r.onerror=()=>reject(new Error('Could not read '+file.name));r.readAsDataURL(file);});
}
async function previewImages(settings,generation){
  if(imageBusy)return;
  imageBusy=true;invalidateImages();$('image-review').hidden=false;$('image-status').textContent='Validating and uploading images…';$('optimize-button').disabled=true;
  try{
    const files=[...$('image-files').files];
    if(!files.length||files.length>20||files.some(f=>f.size>5*1024*1024)||files.reduce((n,f)=>n+f.size,0)>20*1024*1024)throw new Error('Choose 1–20 JPEG/PNG images, maximum 5 MiB each and 20 MiB total.');
    if(!$('image-start').value||!$('image-deadline').value)throw new Error('Set earliest start and deadline.');
    settings.earliest_start=new Date($('image-start').value).toISOString();settings.deadline=new Date($('image-deadline').value).toISOString();
    const max_edge=Number($('image-edge').value),quality=Number($('image-quality').value),timing=$('image-timing').value;
    if(!uploadedBatch){
      const payload={files:await Promise.all(files.map(async f=>({name:f.name,content:await fileContent(f)})))};
      const uploaded=await api('/api/batches',{method:'POST',body:JSON.stringify(payload)});
      if(generation!==planGeneration)return;
      uploadedBatch=uploaded;
    }
    const result=await api('/api/batches/preview',{method:'POST',body:JSON.stringify({plan:settings,batch:{batch_id:uploadedBatch.batch_id,manifest_sha256:uploadedBatch.manifest_sha256,max_edge,quality},timing})});
    if(generation!==planGeneration)return;
    imagePreview=result;imageAttempted=false;
    plan={request:result.request,result:result.comparison};renderPlan();$('plan-output').classList.remove('stale');$('export-plan').disabled=false;
    $('image-review-content').innerHTML=`<p><strong>${uploadedBatch.files.length} images</strong> · ${max_edge}px longest edge · JPEG quality ${quality}</p><dl class="detail-grid"><div><dt>Selected start · ${timing==='earliest'?'earliest permitted':'recommended'}</dt><dd>${esc(stamp(result.result.recommended.start))}</dd></div><div><dt>Reserved until</dt><dd>${esc(stamp(result.result.recommended.finish))}</dd></div></dl><p class="help">The exact files, settings and schedule are saved with this preview. New edits require a new preview.</p>`;
  }catch(e){$('image-status').textContent=e.message;notify(e.message);}
  finally{imageBusy=false;$('optimize-button').disabled=false;syncImageControls();}
}
$('queue-images').onclick=async()=>{
  if(imageBusy||$('queue-images').disabled)return;
  imageBusy=true;imageAttempted=true;syncImageControls();$('image-status').textContent='Queuing saved preview…';
  const preview=imagePreview;
  try{
    const job=await api('/api/batches/queue/'+encodeURIComponent(preview.preview_id),{method:'POST'});
    if(imagePreview===preview)imagePreview=null;
    $('image-status').textContent='Queued '+job.id.slice(0,12)+'. Follow progress and download results in Job activity.';
    await refresh();await openJob(job.id);
  }catch(e){$('image-status').textContent=e.message+' Retry keeps the same preview and cannot create a duplicate job.';notify(e.message);}
  finally{imageBusy=false;syncImageControls();}
};
setInterval(syncImageControls,1000);
updatePlan();poll();
