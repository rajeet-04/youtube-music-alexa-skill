/* First-party aggregates only. No credential fields or diagnostic HTML. */
(function(root) {
  'use strict';
  const number = v => typeof v === 'number' && Number.isFinite(v);
  const formatPercent = v => number(v) ? (v * 100).toFixed(1) + '%' : 'Unavailable';
  const formatSeconds = v => number(v) ? v.toFixed(2) + ' s' : 'Insufficient samples';
  const bytes = v => number(v) ? (v / 1e9).toFixed(3) + ' GB' : 'Unavailable';
  const count = v => number(v) ? v.toLocaleString() : '0';
  const ratio = (a,b) => b ? a/b : null;

  function createController(deps) {
    let inFlight=false, stopped=false, latest=null, windowName='15m', timer=null;
    function show() { if (latest) deps.render(latest,windowName); }
    async function refresh() {
      if (stopped || inFlight || deps.document.hidden) return;
      inFlight=true;
      try {
        const response=await deps.fetch('/admin/api/status',{credentials:'same-origin',cache:'no-store'});
        if (response.status===401 || response.status===503) {
          if (response.status===401) {
            stopped=true; if (timer!==null) deps.clearInterval(timer);
            deps.notice('Session expired. Sign in again at /admin/login.');
            return;
          }
        }
        if (!response.ok) throw Error('unavailable');
        const body=await response.json();
        if (!body.metrics || body.metrics.source!=='jukes') throw Error('invalid snapshot');
        latest=body.metrics;show();deps.notice('Updated '+new Date(deps.now()).toLocaleTimeString());
      } catch (_) { deps.notice('Stale snapshot — refresh unavailable. Retrying while this page is visible.'); }
      finally { inFlight=false; }
    }
    return {refresh,select(name){if(['15m','1h','24h'].includes(name)){windowName=name;show();}},
      start(initial){latest=initial;show();timer=deps.setInterval(refresh,10000);}};
  }

  function render(document,container,m,windowName) {
    const w=m.windows[windowName], c=w.counters, life=m.lifetime;
    const current=m.current, jobs=current.jobs, pools=current.pools, res=m.resources;
    function element(tag,text,className) {
      const e=document.createElement(tag);if(text!==undefined)e.textContent=String(text);
      if(className)e.className=className;return e;
    }
    container.replaceChildren();
    container.append(element('p','Backend '+current.health+' · Metrics collected since '+new Date(m.started_at*1000).toLocaleString()+
      ' · Window coverage '+Math.round(w.coverage_seconds/60)+' min. Counters have 60-second resolution.','muted'));
    const grid=element('div',undefined,'metrics-grid');container.append(grid);
    function panel(title,rows,note) {
      const p=element('div',undefined,'metric-panel');p.append(element('h3',title));
      const table=element('table');p.append(table);
      for(const [label,value] of rows){const tr=element('tr');tr.append(element('td',label),element('td',value));table.append(tr);}
      if(note)p.append(element('p',note,'muted'));grid.append(p);return p;
    }
    const lifetime=(key)=>count(life[key]||0), selected=(key)=>count(c[key]||0);
    const retries=(data)=>(data.retry_fallback||0)+(data.retry_recovery||0);
    panel('Job performance · lifetime',[
      ['Completed extraction jobs',lifetime('job_completed')],['Failed extraction jobs',lifetime('job_failed')],
      ['Retries (total)',count(retries(life))],['Fallback / recovery retries',count(life.retry_fallback||0)+' / '+count(life.retry_recovery||0)],['Queued',count(jobs.jobs.queued)],
      ['Downloading',count(jobs.jobs.downloading)],['Ready',count(jobs.jobs.ready)],
      ['Failed',count(jobs.jobs.failed)],['Evicted',count(jobs.jobs.evicted)]
    ],'Ready/failed/evicted gauges cover retained jobs. Completed totals remain completed after eviction.');
    const perf=panel('Backend source performance · selected window',[
      ['Successful preparations',selected('prepare_success')],['Failed preparations',selected('prepare_failed')],
      ['Average preparation',formatSeconds(w.latency.all.average_seconds)],
      ['P50 preparation',formatSeconds(w.latency.all.p50_seconds)],['P95 preparation',formatSeconds(w.latency.all.p95_seconds)],
      ['P99 preparation',formatSeconds(w.latency.all.p99_seconds)],['Failure rate',formatPercent(w.failure_rate)],
      ['Temporary failures',selected('temporary_failure')],['Terminal failures',selected('terminal_failure')],
      ['Retries',count(retries(c))],['Fallback / recovery retries',count(c.retry_fallback||0)+' / '+count(c.retry_recovery||0)],['Admission rejections',selected('admission_rejected')],
      ['Expired / evicted / missing results',count((c.eviction||0)+(c.warmup_expiration||0)+(c.missing_result||0))]
    ],'Successful preparation samples: '+w.latency.all.sample_count+(w.latency.all.sampled?' (sampled)':'')+
      '. P99 needs 100 samples. '+m.retry_coverage+'. Lifetime preparations: '+lifetime('prepare_success')+' successful / '+lifetime('prepare_failed')+' failed.');
    panel('Preparation latency by path · selected window',[
      ...['cached','warmed','promoted','joined','cold','recovered'].map(key=>[key+' P50 / P95',formatSeconds(w.latency[key].p50_seconds)+' / '+formatSeconds(w.latency[key].p95_seconds)+' ('+w.latency[key].sample_count+' samples'+(w.latency[key].sampled?', sampled':'')+')']),
      ['Queue wait P95',formatSeconds(w.latency.queue.p95_seconds)],['Extraction + validation P95',formatSeconds(w.latency.extraction.p95_seconds)],
      ['Dropped latency observations',selected('latency_dropped')],['Interrupted outcome unknown',selected('prepare_outcome_unknown')]
    ],'Promoted = speculative in-flight work; joined = other in-flight work. Cold includes metadata lookup and queue wait. Recovered latency includes downtime; path differences do not prove causal savings.');
    panel('Audio cache effectiveness · selected window',[
      ['Cache hits',selected('cache_hit')],['Cache misses',selected('cache_miss')],['Audio hit rate',formatPercent(w.cache_hit_rate)],
      ['Main cache hits',selected('main_hit')],['Warmup cache hits',selected('warmup_hit')],['Warmup hit share',formatPercent(w.warmup_hit_rate)],
      ['Joined requests',selected('joined')],['Capacity evictions',selected('eviction')],['Missing results',selected('missing_result')]
    ],'One observation per preparation. Polls, HEAD, range reads and metadata-cache lookups excluded. Lifetime: '+lifetime('cache_hit')+' hits / '+lifetime('cache_miss')+' misses; hit rate '+formatPercent(ratio(life.cache_hit||0,(life.cache_hit||0)+(life.cache_miss||0)))+'.');
    for(const name of ['requested','warmup']) {
      const pool=pools[name];const p=panel(name==='requested'?'Main LRU cache · current':'Warmup cache · current',[
        ['Storage / limit',bytes(pool.used_bytes)+' / '+bytes(pool.limit_bytes)],['Complete files',bytes(pool.ready_bytes)],
        ['Reserved / in flight',bytes(pool.reserved_bytes)],['Entries',count(pool.entries)],
        ['Average entry age',number(pool.average_age_seconds)?Math.round(pool.average_age_seconds)+' s':'Unavailable'],
        ['TTL',pool.ttl_seconds===null?'No TTL':pool.ttl_seconds+' s']
      ]);bar(p,ratio(pool.used_bytes,pool.limit_bytes));
    }
    panel('Warmup efficiency · selected window',[
      ['Warmup requests',selected('warmup_request')],['Unique speculative jobs',selected('warmup_started')],
      ['Successful warmups',selected('warmup_success')],['Failed warmups',selected('warmup_failed')],
      ['Warmed tracks consumed',selected('warmup_consumed')],['Completed warmup hits',selected('warmup_hit')],
      ['In-flight promotions',selected('warmup_inflight')],['Requested warmup-hit share',formatPercent(w.warmup_hit_rate)],
      ['Completion-cohort usefulness',formatPercent(w.warmup_cohort.usefulness)],['Warmup evictions',selected('warmup_eviction')],
      ['Warmup expirations',selected('warmup_expiration')],['Duplicate / cached requests',count((c.warmup_duplicate||0)+(c.warmup_cached||0))]
    ],'Cohort: '+w.warmup_cohort.consumed+' consumed / '+w.warmup_cohort.completed+' completed'+(w.warmup_cohort.maturing?' (still maturing)':'')+
      '. Lifetime: '+lifetime('warmup_request')+' requests, '+lifetime('warmup_success')+' successful, '+lifetime('warmup_consumed')+' consumed; usefulness '+formatPercent(ratio(life.warmup_consumed||0,life.warmup_success||0))+'.');
    const capacity=panel('Queue and workers · current',[
      ['Queue / capacity',count(jobs.jobs.queued)+' / '+count(jobs.queue_limit)],['Queue utilization',formatPercent(jobs.queue_utilization)],
      ['Active / configured workers',count(jobs.active_workers)+' / '+count(jobs.workers)],['Worker utilization',formatPercent(jobs.worker_utilization)]
    ]);bar(capacity,jobs.queue_utilization);bar(capacity,jobs.worker_utilization);
    panel('Local resources · current',[
      ['CPU usage',number(res.cpu.percent)?res.cpu.percent.toFixed(1)+'%':'Unavailable'],['Effective CPU cores',String(res.cpu.effective_cores)],
      ['RAM used / limit',bytes(res.memory.used_bytes)+' / '+bytes(res.memory.limit_bytes)],
      ['Process RAM',bytes(res.memory.process_bytes)],['Disk used / total',bytes(res.disk.used_bytes)+' / '+bytes(res.disk.total_bytes)],
      ['Disk free',bytes(res.disk.free_bytes)],['Network RX / TX',bytes(res.network.rx_bytes)+' / '+bytes(res.network.tx_bytes)],
      ['RX / TX rate',speed(res.network.rx_bytes_per_second)+' / '+speed(res.network.tx_bytes_per_second)]
    ],'CPU: '+res.cpu.scope+'. RAM: '+res.memory.scope+'. Network: '+res.network.scope+'. Rates need two samples; network throughput is not a bandwidth limit.');
    const trends=element('div',undefined,'trends');container.append(element('h3','Rolling trends · selected window'),trends);
    for(const [key,label,fmt] of [['average_seconds','Mean preparation',formatSeconds],['failure_rate','Failure rate',formatPercent],
      ['cache_hit_rate','Audio hit rate',formatPercent],['warmup_consumed','Warmup consumption',count]]) {
      const p=element('div',undefined,'trend');trends.append(p);p.append(element('span',label));
      const points=w.trends.filter(t=>number(t[key]));
      if(!points.length){p.append(element('p','No samples','muted'));continue;}
      const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 240 42');
      svg.setAttribute('role','img');svg.setAttribute('aria-label',label+' trend; latest '+fmt(points.at(-1)[key]));
      const max=Math.max(...points.map(t=>t[key]),0.001);const minAt=points[0].at,maxAt=points.at(-1).at;
      const line=document.createElementNS('http://www.w3.org/2000/svg','polyline');
      line.setAttribute('points',points.filter((_,i)=>i%Math.max(1,Math.ceil(points.length/120))===0 || i===points.length-1)
        .map(t=>(4+(t.at-minAt)/Math.max(60,maxAt-minAt)*232)+','+(38-t[key]/max*34)).join(' '));
      line.setAttribute('fill','none');line.setAttribute('stroke','#287');line.setAttribute('stroke-width','2');svg.append(line);p.append(svg);
      p.append(element('span','Latest '+fmt(points.at(-1)[key])+' · '+points.length+' active minutes','muted'));
    }
    function bar(parent,value){const b=element('div',undefined,'bar');const fill=element('i');fill.style.width=(number(value)?Math.min(100,Math.max(0,value*100)):0)+'%';b.append(fill);parent.append(b);}
    function speed(v){return number(v)?(v/1e6).toFixed(3)+' MB/s':'Unavailable';}
  }
  if(typeof module==='object' && module.exports) module.exports={createController,formatPercent,formatSeconds,render};
  if(root && root.document) {
    const d=root.document,initial=d.getElementById('metrics-initial');if(!initial)return;
    const controller=createController({document:d,fetch:root.fetch.bind(root),render:(m,w)=>render(d,d.getElementById('metrics-content'),m,w),
      notice:text=>{d.getElementById('metrics-freshness').textContent=text;},setInterval:root.setInterval.bind(root),clearInterval:root.clearInterval.bind(root),now:Date.now});
    controller.start(JSON.parse(initial.textContent));
    d.getElementById('metrics-window').addEventListener('change',e=>controller.select(e.target.value));
    d.addEventListener('visibilitychange',()=>{if(!d.hidden)controller.refresh();});
  }
})(typeof window==='undefined'?null:window);
