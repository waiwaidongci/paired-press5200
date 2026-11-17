import {download} from '../utils/download.js';
async function authFetch(path,options={}){
 const token=localStorage.getItem('research-token');
 const response=await fetch('/api'+path,{...options,headers:{'Content-Type':'application/json',...(token?{Authorization':'Bearer '+token}:{}),...options.headers}});
 if(!response.ok){const x=await response.json().catch(()=>({detail:'网络错误，请重试'}));if(response.status===401){localStorage.clear();location.reload();}throw new Error(typeof x.detail==='string'?x.detail:JSON.stringify(x.detail));}
 return response;
}
async function upload(path,file){
 const token=localStorage.getItem('research-token');
 const form=new FormData();form.append('file',file);
 const response=await fetch('/api'+path,{method:'POST',headers:token?{Authorization:'Bearer '+token}:{},body:form});
 if(!response.ok){const x=await response.json().catch(()=>({detail:'网络错误，请重试'}));throw new Error(typeof x.detail==='string'?x.detail:JSON.stringify(x.detail));}
 return response.json();
}
export const packages={
 exportZip:async(studyId)=>download(await (await authFetch(`/packages/studies/${studyId}/export`)).blob(),`research-package-${studyId}.zip`),
 preview:file=>upload('/packages/preview',file),
 import:file=>upload('/packages/import',file),
 imports:async()=>(await authFetch('/packages/imports')).json()
};
