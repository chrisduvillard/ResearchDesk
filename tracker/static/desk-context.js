/* Identity and routes shared by every full contributor/fund dashboard. */
"use strict";
(() => {
  const parts=location.pathname.split('/').filter(Boolean);
  const fund=parts[0]==='funds'||parts[0]==='dbmf';
  const id=fund?(parts[1]||'DBMF'):(parts[1]||'dan-nathan');
  const legacyFund=fund&&id==='DBMF';
  const scope=fund?(legacyFund?'dbmf':'fund:'+id):(id==='dan-nathan'?'dan':'contributor:'+id);
  const path=fund?(legacyFund?'/dbmf':'/funds/'+encodeURIComponent(id)):(id==='dan-nathan'?'/dan':'/contributors/'+encodeURIComponent(id));
  const api=legacyFund?'/api/dbmf':'/api/v2/'+(fund?'funds/':'contributors/')+encodeURIComponent(id)+'/desk';
  const context={id,fund,legacyFund,scope,path,api,name:id,equity:false,
    reportId:value=>value?(legacyFund?Number(value):String(value)):null,
    url(url){
      if(url==='/api/desk/health')return '/api/v2/'+(fund?'funds/':'contributors/')+encodeURIComponent(id)+'/desk/health';
      if(legacyFund&&url==='/api/dbmf/status')return '/api/v2/funds/DBMF/desk/status';
      if(fund)return url.replace(/^\/api\/dbmf(?=\/)/,api);
      return url.replace(/^\/api\/(?=(?:status|positions|instruments|events|timeline|prices|scorecard|analysis|changes|export)(?:\/|\?|$))/,api+'/');
    },
    apply(status){
      context.name=status.name||id;context.equity=!!status.equity;
      document.title=context.name+(fund?' · Exposure Desk':' · Disclosure Desk');
      const home=document.querySelector('.brand');if(home&&fund)home.href=path;
      const brand=document.querySelector('.brand small');
      if(brand)brand.textContent=context.name.toUpperCase();
      const mark=document.querySelector('.brand-mark');
      if(mark){mark.textContent=fund?id.slice(0,1)+id.slice(1,2).toLowerCase():context.name.split(' ').map(s=>s[0]).join('');const arrow=document.createElement('span');arrow.textContent='↗';mark.append(arrow);}
      const heading=document.querySelector(fund?'.page-heading h1':'#page-overview h1');
      if(heading){heading.textContent=context.name+(fund?(context.equity?'’s holdings':'’s market exposure'):'’s positions');const dot=document.createElement('span');dot.className='title-dot';dot.textContent='.';heading.append(dot);}
      const source=document.querySelector('.page-heading a.text-link');
      if(source&&/^https:\/\//.test(status.source_url||''))source.href=status.source_url;
      if(!fund){
        const hint=document.querySelector('#page-overview .page-heading .muted');if(hint)hint.textContent='Track disclosed direction. See what happens next.';
        let calls=document.querySelector('#contributor-calls');
        if(!calls){calls=document.createElement('a');calls.id='contributor-calls';calls.className='text-link';source?.after(calls);}
        calls.href='/research?view=calls&contributor='+encodeURIComponent(id);calls.textContent='Reviewed calls →';
      }
      for(const a of document.querySelectorAll('a[href^="/api/"]'))a.setAttribute('href',context.url(a.getAttribute('href')));
    }
  };
  window.DeskContext=context;
})();
