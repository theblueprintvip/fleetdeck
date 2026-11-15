  // First open: three quick questions (who, where, upload or generate), then normal chat.
  const WHO = ['Athlete / NIL', 'Creator', 'Musician / entertainer', 'Job offer / everyday', 'Renter / homeowner', 'Business / freelance'];
  function onboard() {
    const box = document.createElement('div');
    box.className = 'msg bot ob';
    log.appendChild(box);
    const p = profile();
    const save = () => {
      store.set('rl-profile', JSON.stringify(p));
      const cloud = window.RedlineCloud;
      if (cloud && cloud.user) cloud.saveMe({ who: p.who, city: p.city, state: p.state, share_stats: store.get('rl-share') !== '0' }).catch(() => {});
    };
    const step1 = () => {
      box.innerHTML = `<span>Hey, I’m Tila. Before we look at anything: who am I helping today?</span><div class="chips-pick">${WHO.map(w => `<button type="button" class="pick">${esc(w)}</button>`).join('')}</div>`;
      box.querySelectorAll('.pick').forEach(b => b.addEventListener('click', () => { p.who = b.textContent; step2(); }));
    };
    const step2 = () => {
      box.innerHTML = '<span>Got it. Where are you signing from? It decides which rules apply.</span>'
        + '<div class="two"><input id="obCity" placeholder="City" aria-label="City"><input id="obState" placeholder="State" aria-label="State or region"></div>'
        + '<span class="err" id="obErr" hidden>Enter both a city and a state first.</span><button type="button" class="go" id="obGo">Continue</button>';
      box.querySelector('#obGo').addEventListener('click', () => {
        const city = box.querySelector('#obCity').value.trim();
        const st = box.querySelector('#obState').value.trim();
        if (!city || !st) { box.querySelector('#obErr').hidden = false; return; }
        p.city = city;
        p.state = st;
        step3();
      });
    };
    const step3 = () => {
      box.innerHTML = '<span>Last one: do you already have a contract, or do you need one made?</span>'
        + '<div class="chips-pick"><button type="button" class="pick" data-m="upload">Upload a contract</button><button type="button" class="pick" data-m="create">Generate one</button></div>';
      box.querySelectorAll('.pick').forEach(b => b.addEventListener('click', () => {
        save();
        const upload = b.dataset.m === 'upload';
        box.classList.remove('ob');
        box.textContent = `Perfect: ${p.who}, signing from ${p.city}, ${p.state}. ` + (upload ? 'Drop your contract in and I’ll review every clause.' : 'Fill in the six fields and I’ll draft a safe one.');
        document.getElementById(upload ? 'scan' : 'create').scrollIntoView({ behavior: 'smooth' });
        if (upload) fileInput.click();
      }));
    };
    step1();
  }
