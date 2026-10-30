'use strict';
const http=require('node:http');
const {chromium}=require('playwright');
let server=null,health=null,stopping=false;
async function shutdown(){
 if(stopping)return;stopping=true;
 if(health)health.close();
 try{if(server)await server.close();}finally{process.exit(0);}
}
(async()=>{
 const key=process.env.BROWSER_WS_PATH;
 if(!/^[a-f0-9]{64}$/.test(key||''))throw Error('Invalid browser secret');
 server=await chromium.launchServer({headless:true,chromiumSandbox:true,host:'0.0.0.0',port:3000,wsPath:key,timeout:60000});
 server.on('close',()=>{if(!stopping)process.exit(1);});
 health=http.createServer((req,res)=>{
  if(req.method==='GET'&&req.url==='/health/ready'){
   const ready=!stopping&&server&&server.process().exitCode===null;
   res.writeHead(ready?200:503,{'Content-Type':'application/json'});res.end(JSON.stringify({ready}));
  }else{res.writeHead(404);res.end();}
 });
 health.listen(3001,'0.0.0.0',()=>console.log('Browser service ready'));
})().catch(()=>{console.error('Browser startup failed; inspect sandbox and image compatibility');process.exit(1);});
process.on('SIGTERM',shutdown);process.on('SIGINT',shutdown);



