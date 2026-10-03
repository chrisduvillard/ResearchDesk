/* Owner review stays available on every full contributor dashboard. */
"use strict";
(() => {
  let authenticated=false;
  const button=document.createElement('button');
  button.id='review-direction';button.className='button secondary hidden';button.textContent='Review direction';button.hidden=true;
  document.querySelector('.chart-top').append(button);
  const dialog=document.createElement('dialog');dialog.id='direction-review';dialog.setAttribute('aria-labelledby','direction-review-title');
  dialog.innerHTML='<div class="dialog-heading"><h2 id="direction-review-title">Review direction</h2><button type="button" class="icon-button" aria-label="Close direction review">×</button></div><form style="display:grid;gap:16px"><label>Reviewed direction<select name="direction" aria-label="Reviewed direction"><option value="bullish">Bullish</option><option value="bearish">Bearish</option><option value="mixed">Mixed</option><option value="unknown">Unknown</option><option value="non_directional">No simple direction</option><option value="large_move">Large moves</option><option value="range_bound">Price range</option><option value="conditional">Conditional</option></select></label><label>Review reason<textarea name="reason" maxlength="2000" required style="display:block;width:100%;min-height:100px"></textarea></label><p role="alert"></p><button class="button dark" type="submit">Save review</button></form>';
  document.body.append(dialog);
  const form=dialog.querySelector('form'), message=form.querySelector('[role=alert]');
  let selected=null;
  window.DeskOwner={selection(){button.hidden=!authenticated||!DeskContext.selected;button.classList.toggle('hidden',button.hidden);button.disabled=!DeskContext.reviewable;button.title=DeskContext.reviewable?'Review this disclosed direction':'Review requires one current disclosed strategy';}};
  async function session(){const r=await fetch('/api/v2/auth',{cache:'no-store'});if(!r.ok)throw Error('Could not check the owner session');return r.json();}
  session().then(a=>{authenticated=a.authenticated;DeskOwner.selection();}).catch(()=>{});
  button.addEventListener('click',()=>{selected=DeskContext.selected;form.reset();form.elements.direction.value=DeskContext.direction||'unknown';message.textContent='';document.querySelector('#direction-review-title').textContent='Review '+selected;dialog.showModal();});
  dialog.querySelector('.icon-button').addEventListener('click',()=>dialog.close());
  form.addEventListener('submit',async event=>{
    event.preventDefault();const submit=form.querySelector('[type=submit]');submit.disabled=true;
    try {
      const auth=await session();
      const response=await fetch('/api/v2/contributors/'+encodeURIComponent(DeskContext.id)+'/reviews',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':auth.csrf_token||''},body:JSON.stringify({symbol:selected,direction:form.elements.direction.value,reason:form.elements.reason.value})});
      if(!response.ok){const error=await response.json();throw Error(typeof error.detail==='string'?error.detail:'The review could not be saved');}
      dialog.close();window.dispatchEvent(new CustomEvent('contributor-review-saved'));
    } catch(error){message.textContent=error.message;}
    finally {submit.disabled=false;}
  });
})();
