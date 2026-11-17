import {DOMAIN} from '../constants/domain.js';
import {store,load} from '../stores/research.js';
import {packages} from '../api/packages.js';
import {studySelect} from '../components/common/study-select.js';
import {table} from '../components/common/table.js';
import {esc} from '../utils/format.js';

export async function render(){
 const role=localStorage.getItem('research-role');
 const writable=role!=='viewer';
 const batches=await packages.imports().catch(()=>[]);
 return `<div class="eyebrow">06 / PACKAGE MIGRATION</div>
 <h1>异地研究包迁入迁出</h1>
 <p class="lead">研究包取同一时点，含纸样、冻结输入、分析、复核与计算留档。迁出只读取登记，登记仍可继续；迁入核验引用与留档校验，编号按来源映射，同名异源并存，同源重试不新增，内容变动拒绝且原项不变。</p>
 <div class="method-note">失败无半项可见：数据库与留档齐备后原子发布；中断恢复与并发迁入沿用同一映射。已删除的纸样仍保留在运行快照中，历史分析可完整还原。</div>
 ${writable?`<section class="worksheet"><h2>迁出研究包</h2><div class="inline-form">${studySelect()}<button type="button" id="export-package">打包并下载 ZIP</button></div><small>同一时点快照：来源纸样、运行冻结输入与SHA256、结果、复核、计算留档。</small></section>`:''}
 <section class="worksheet"><h2>迁入研究包</h2>
   <label>选择研究包 ZIP<input type="file" id="package-file" accept=".zip,application/zip"></label>
   <div class="inline-form"><button type="button" id="preview-package">核对编号碰撞与坏包</button>${role==='admin'?'<button type="button" id="import-package" disabled>确认迁入</button>':''}</div>
   <div id="preview-box" class="method-note" hidden></div>
 </section>
 <h2>迁入登记 <small>同一来源重试沿用映射，不新增项目</small></h2>
 ${table(['来源站点','来源研究编号','状态','本地映射研究','纸样/运行/复核/留档','碰撞说明'],batches.map(b=>[
   esc(b.source_station),b.source_study_id,
   {html:`<span class="status ${b.status==='completed'?'completed':''}">${b.status==='completed'?'已完成':b.status==='staging'?'中断可恢复':'已拒绝'}</span>`},
   b.new_study_id??'—',
   `${b.counts.observation||0}/${b.counts.run||0}/${b.counts.review||0}/${b.counts.artifact||0}`,
   b.conflict_note||b.error||'—'
 ]))}`;
}

export function bind(refresh){
 const exportBtn=document.getElementById('export-package');
 if(exportBtn)exportBtn.onclick=async()=>{
   exportBtn.disabled=true;
   try{await packages.exportZip(store.studyId);}catch(e){document.getElementById('notice').textContent=e.message;}
   finally{exportBtn.disabled=false;}
 };
 const sel=document.getElementById('study-select');
 if(sel)sel.onchange=()=>{store.studyId=+sel.value;refresh();};
 const fileInput=document.getElementById('package-file');
 const box=document.getElementById('preview-box');
 const importBtn=document.getElementById('import-package');
 let previewData=null;
 document.getElementById('preview-package').onclick=async()=>{
   if(!fileInput.files[0]){document.getElementById('notice').textContent='请先选择研究包 ZIP';return;}
   box.hidden=false;box.textContent='正在核验研究包…';
   try{
     previewData=await packages.preview(fileInput.files[0]);
     const c=previewData;
     box.innerHTML=`<strong>${esc(c.source_station)} · 来源研究 #${c.source_study_id} · ${esc(c.study_name)}</strong>
       <p>纸样 ${c.counts.observations} · 运行 ${c.counts.runs} · 复核 ${c.counts.reviews} · 留档 ${c.counts.artifacts}</p>
       <p>包校验 ${esc(c.package_checksum.slice(0,16))}…</p>
       <p>${c.content_changed?'<b style="color:oklch(45% .16 25)">同源内容已变动：迁入将被拒绝，原项不变</b>':
          c.same_source_state?`同源登记已存在（${c.same_source_state}）：重试不新增，沿用原映射`:
          '新来源：迁入将建立新的编号映射'}</p>
       <p>${c.name_collision?'本地存在同名研究：同名异源并存，迁入名称将追加来源站点后缀':'本地无同名研究，名称可直接使用'}</p>`;
     if(importBtn)importBtn.disabled=c.content_changed;
   }catch(e){previewData=null;box.textContent='核验未通过：'+e.message;if(importBtn)importBtn.disabled=true;}
 };
 if(importBtn)importBtn.onclick=async()=>{
   if(!fileInput.files[0])return;
   importBtn.disabled=true;box.hidden=false;box.textContent='正在原子迁入（数据库与留档齐备后发布）…';
   try{
     const r=await packages.import(fileInput.files[0]);
     box.innerHTML=`<strong>迁入${r.idempotent?'未重复创建（同源重试）':'完成'}</strong>
       <p>本地研究编号 #${r.new_study_id} · 映射纸样 ${r.counts.observation||0} / 运行 ${r.counts.run||0} / 复核 ${r.counts.review||0}</p>
       <p>${esc(r.conflict_note||'无编号碰撞')}</p>`;
     await load();refresh();
   }catch(e){box.textContent='迁入失败（无半项可见，原项不变）：'+e.message;importBtn.disabled=false;}
 };
}
