// UI fixture only: intercept every API request; never submit a production run.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const puppeteer = require('../../runtime/project-browser/node_modules/puppeteer-core');
const origin = process.env.UIUX_ORIGIN || 'http://127.0.0.1:5186';
const output = path.resolve(__dirname, '../../docs/design/requirements-2026-09-15');
const run = { id:'requirement-demo',project_id:'project',status:'awaiting_spec_confirmation',revision:1,request:'做一个像美团的模拟购物工具',source:{type:'web',operation:'general',actor_id:1},created_at:'2026-09-15T06:00:00Z',updated_at:'2026-09-15T06:00:00Z',tasks:[],history:[],plan:null,artifacts:{},spec_draft:{goal:'模拟团购点单，体验购物流程而不产生真实交易',screens:[{name:'首页',purpose:'搜索、分类与浏览商品'}],flows:['浏览商品，加入购物车，模拟提交订单'],data_model:['商品：名称、价格、图片；订单：条目、金额、状态'],non_goals:['不接入真实支付，不实际配送'],risks_assumptions:['用示例商户与商品数据；视觉参照来自模型知识']},recommended_skills:[{id:'style',version:1,reason:'明确页面层级与控件状态'}],requirement_skill_catalog:[{id:'style',version:1,name:'清晰界面规范'}],fidelity_target:{reference:'美团',basis:'模型知识与用户描述，未抓取外站；以确认后的标尺为准',screens:[{screen:'首页',layout:['顶部搜索、分类入口、下方商品列表'],colors:['黄色主色，浅色卡面与深色正文'],components:['搜索框、分类导航、商品卡片'],interactions:['分类切换筛选商品']}]} };
function payload(p) {
 if(p==='/api/auth/me') return {user:{id:1,username:'owner',role:'admin'},csrf_token:'fixture'};
 if(p==='/api/v3/environment') return {mode:'preview',label:'隔离界面演练'};
 if(p==='/api/v2/projects') return {projects:[{id:'project',name:'模拟团购'}]};
 if(p==='/api/v2/runs/'+run.id) return run;
 if(p.endsWith('/runs')) return {runs:[run]};
 if(p.endsWith('/conversation')) return {messages:[]};
 if(p.endsWith('/events')) return {events:[],cursor:0};
 if(p.endsWith('/plans')) return {versions:[]};
 if(p.endsWith('/deliverables')) return {items:[],count:0};
 return {};
}
(async()=>{
 fs.mkdirSync(output,{recursive:true});
 const browser=await puppeteer.launch({headless:true,executablePath:process.env.BROWSER_EXECUTABLE||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 const errors=[],report=[];
 try{
 for(const theme of ['light','dark']){
  run.status='awaiting_spec_confirmation';
  const page=await browser.newPage(); await page.setViewport({width:1440,height:1000});
  await page.evaluateOnNewDocument(t=>localStorage.setItem('webuddy:theme',t),theme);
  page.on('pageerror',e=>errors.push(e.message));await page.setRequestInterception(true);
  const submissions=[];
  page.on('request',async req=>{
   const url=new URL(req.url());
   if(url.pathname.startsWith('/api/')){
    if(req.method()==='POST') {assert.equal(url.pathname,`/api/v2/runs/${run.id}/confirm-spec`);submissions.push(JSON.parse(req.postData()));run.status='received';}
    await req.respond({status:200,contentType:'application/json',body:JSON.stringify(req.method()==='POST'?{...run,status:'received'}:payload(url.pathname))});
   }else if(url.origin===origin||url.protocol==='data:')await req.continue();else await req.abort();
  });
  await page.goto(origin+'/runs/'+run.id,{waitUntil:'networkidle0'});
  await page.waitForSelector('section[aria-label="确认需求后开工"]');
  const form='section[aria-label="确认需求后开工"]';
  assert.equal(await page.$$eval(form+' form',els=>els.length),1);
  assert.equal(await page.$eval(form+' input[type=checkbox]',e=>e.checked),true);
  await page.screenshot({path:path.join(output,`confirmation-${theme}.png`),fullPage:true});
  await page.setViewport({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  await page.click(form+' button[value=edit_start]');
  await page.waitForFunction(()=>!document.querySelector('section[aria-label="确认需求后开工"]'));
  assert.equal(submissions.length,1);assert.equal(submissions[0].selected_skills[0].id,'style');assert.equal(submissions[0].fidelity_target.reference,'美团');
  report.push({theme,oneForm:true,oneConfirmationRequest:true,skillsAndFidelitySubmitted:true,mobileOverflow:false});
  await page.close();
 }
 assert.deepEqual(errors,[]);fs.writeFileSync(path.join(output,'verification.json'),JSON.stringify({fixture:true,report,errors},null,2));
 console.log('Requirement confirmation browser checks passed in light/dark and mobile.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
