import {api} from '../api.js';
import {state,pageSize} from '../state.js';
import {el,button,heading,table,notice,empty,jsonView} from '../lib/html.js';
import {icon,registerIcons} from '../lib/icons.js';
registerIcons({'span-failed':'M12 3 2 20h20L12 3z M12 10v4 M12 17h.01'});
// Decimal-point shifting preserves every digit of server-priced costs.
export function cents(value){if(value===null||value===undefined)return 'Unknown';const match=String(value).match(/^([+-]?)(\d+)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/);if(!match)return String(value)+' USD';const digits=match[2]+(match[3]||''),point=match[2].length+Number(match[4]||0)+2;if(Math.abs(point)>1000)return String(value)+' USD';const shifted=point<=0?'0.'+'0'.repeat(-point)+digits:point>=digits.length?digits+'0'.repeat(point-digits.length):digits.slice(0,point)+'.'+digits.slice(point);return match[1]+shifted.replace(/^0+(?=\d)/,'')+' ¢';}
// Recorded nanosecond spans rendered as readable elapsed time; negative ranges are reported as unknown.
function elapsed(ns){if(ns<0n)return 'unknown';const value=Number(ns);if(value<1e9)return `${(value/1e6).toFixed(value<1e7?2:0)} ms`;return `${(value/1e9).toFixed(value<6e10?2:0)} s`;}
export async function render(container){
  const controller=new AbortController(),instance=state.instanceId,list=el('div'),detail=el('section',{className:'card stack',hidden:true});let cursor=null,busy=false;
  const next=button('Older traces',()=>load(cursor),'button ghost');container.append(heading('Observability','Trace waterfalls and exact recorded AI costs.'),button('Refresh',()=>load(),'button ghost'),list,next,detail);
  async function inspect(row,after=null){detail.hidden=false;try{const params=new URLSearchParams({instance_id:row.instance_id,limit:String(pageSize())});if(after)params.set('after',after);const result=await api('/api/otel/traces/'+encodeURIComponent(row.trace_id)+'?'+params,{signal:controller.signal});
      detail.replaceChildren(el('h2',{},'Trace '+row.trace_id),el('p',{className:'muted'},'Costs and tokens below cover this returned page; estimates retain their original pricing provenance.'),el('div',{className:'card'},el('strong',{},'Known cost: '+cents(result.known_cost_usd)),el('p',{},`Total: ${cents(result.cost_usd)} · ${result.unknown_cost_spans} span(s) with unknown cost`)));
      if(!result.data.length){detail.append(empty('No further spans','This trace has no spans on this page.'));return;}
      const start=result.data.reduce((min,span)=>BigInt(span.start_ns)<min?BigInt(span.start_ns):min,BigInt(result.data[0].start_ns));const end=result.data.reduce((max,span)=>BigInt(span.end_ns)>max?BigInt(span.end_ns):max,start+1n);const duration=end-start||1n;
      for(const span of result.data){const offset=Number((BigInt(span.start_ns)-start)*10000n/duration)/100,width=Math.max(.3,Number((BigInt(span.end_ns)-BigInt(span.start_ns))*10000n/duration)/100);
        // Failed matches the server's error-span count: recorded status or classified health is 'error'.
        const status=span.health_status||span.status||'Unknown',failed=span.status==='error'||span.health_status==='error';
        const description=`${span.name}: ${failed?'failed, ':''}starts at ${elapsed(BigInt(span.start_ns)-start)}, duration ${elapsed(BigInt(span.end_ns)-BigInt(span.start_ns))}, status ${status}`;
        const bar=el('div',{className:failed?'waterfall-bar failed':'waterfall-bar',role:'img','aria-label':description,title:description});bar.style.marginLeft=`${Math.max(0,Math.min(100,offset))}%`;bar.style.width=`${Math.max(0,Math.min(100-offset,width))}%`;
        const info=el('details',{},el('summary',{},failed?el('span',{className:'span-failed'},icon('span-failed'),'Failed'):null,`${span.name} · ${status} · ${cents(span.cost_exact)}`),el('p',{className:'muted'},`Model: ${span.model||'Unreported'} · Input tokens: ${span.tokens_in??'Unknown'} · Output tokens: ${span.tokens_out??'Unknown'}`),jsonView({pricing:span.pricing,attributes:span.attributes,parent_id:span.parent_id}));detail.append(el('div',{className:failed?'stack waterfall-span failed':'stack waterfall-span'},info,el('div',{className:'waterfall-track'},bar)));
      }if(result.next_cursor)detail.append(button('Next span page',()=>inspect(row,result.next_cursor),'button ghost'));
    }catch(error){if(!controller.signal.aborted)notice(detail,error);}}
  async function load(before=null){if(busy)return;busy=true;next.disabled=true;try{const params=new URLSearchParams({limit:String(pageSize())});if(instance)params.set('instance_id',instance);if(before)params.set('before',before);const result=await api('/api/otel/traces?'+params,{signal:controller.signal});cursor=result.next_cursor;list.replaceChildren(result.data.length?table(['Trace','Instance','Spans','Error spans',''],result.data.map(row=>[row.trace_id,state.instances.find(item=>item.id===row.instance_id)?.name??row.instance_id,String(row.span_count),String(row.error_spans),button('View waterfall',()=>inspect(row),'button ghost')]),{numeric:[2,3]}):empty('Waiting for telemetry','Create an exporter token in Instances and connect your n8n telemetry exporter.'));}catch(error){if(!controller.signal.aborted)notice(list,error);}finally{busy=false;next.disabled=!cursor;}}
  await load();return()=>controller.abort();
}
