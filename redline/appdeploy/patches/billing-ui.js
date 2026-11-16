  // ---- Plans and payments (Stripe Checkout via the backend) ----
  const PRICE_TEXT = {
    en: 'Scans are free: the score, every flag and the first safe rewrite. A full review with every rewrite and a counter-offer is $9, or $19 for 3 and $39 for 10. Pro is $19 a month or $144 a year for unlimited full reviews.',
    es: 'El análisis es gratis: la puntuación, cada alerta y la primera redacción segura. Una revisión completa con todas las redacciones y una contraoferta cuesta 9 $, o 19 $ por 3 y 39 $ por 10. Pro cuesta 19 $ al mes o 144 $ al año, ilimitado.',
    fr: 'L’analyse est gratuite : le score, chaque alerte et la première réécriture. Une revue complète avec toutes les réécritures et une contre-proposition coûte 9 $, ou 19 $ les 3 et 39 $ les 10. Pro : 19 $ par mois ou 144 $ par an, en illimité.',
    de: 'Der Scan ist kostenlos: Risikowert, jede Warnung und die erste sichere Formulierung. Eine vollständige Prüfung mit allen Formulierungen und Gegenangebot kostet 9 $, 3 Stück 19 $, 10 Stück 39 $. Pro: 19 $ im Monat oder 144 $ im Jahr, unbegrenzt.',
    pt: 'A análise é grátis: a nota, cada alerta e a primeira redação segura. Uma revisão completa com todas as redações e contraproposta custa US$ 9, ou US$ 19 por 3 e US$ 39 por 10. Pro: US$ 19 por mês ou US$ 144 por ano, ilimitado.',
    it: 'L’analisi è gratuita: punteggio, ogni avviso e la prima riformulazione. Una revisione completa con tutte le riformulazioni e una controproposta costa 9 $, oppure 19 $ per 3 e 39 $ per 10. Pro: 19 $ al mese o 144 $ all’anno, illimitato.',
    nl: 'Scannen is gratis: de score, elke waarschuwing en de eerste veilige herformulering. Een volledige review met alle herformuleringen en een tegenvoorstel kost $9, of $19 voor 3 en $39 voor 10. Pro: $19 per maand of $144 per jaar, onbeperkt.',
    ar: 'الفحص مجاني: الدرجة وكل تنبيه وأول صياغة آمنة. المراجعة الكاملة مع كل الصياغات والعرض المضاد بـ 9 دولارات، أو 19 دولارًا لثلاث و39 دولارًا لعشر. Pro بـ 19 دولارًا شهريًا أو 144 دولارًا سنويًا بلا حدود.',
    zh: '扫描免费：风险分、每个提示和第一条安全改写。完整审阅（全部改写和还价邮件）9 美元，3 次 19 美元，10 次 39 美元。Pro 每月 19 美元或每年 144 美元，不限次数。',
    ja: 'スキャンは無料：スコア、すべての警告、最初の安全な書き換え。全書き換えと対案メール付きのフルレビューは 9 ドル、3 回 19 ドル、10 回 39 ドル。Pro は月 19 ドルまたは年 144 ドルで無制限。',
  };
  Object.keys(PRICE_TEXT).forEach(k => { if (L[k]) L[k].price = PRICE_TEXT[k]; });

  function showPlans(msg) {
    const note = document.getElementById('planNote');
    note.textContent = msg;
    note.hidden = false;
    document.getElementById('pricing').scrollIntoView({ behavior: 'smooth' });
  }

  async function buy(product) {
    const cloud = window.RedlineCloud;
    if (!cloud) return;
    if (!cloud.user) {
      store.set('rl-buy', product);
      cloud.signIn().catch(() => {});
      return;
    }
    try {
      const r = await cloud.checkout(product);
      location.href = r.url;
    } catch (e) {
      showPlans(errMsg(e));
    }
  }
  document.querySelectorAll('[data-buy]').forEach(b => b.addEventListener('click', () => buy(b.dataset.buy)));

  async function unlockReview(d) {
    const cloud = window.RedlineCloud;
    if (!cloud || !cloud.user || !d.scan_id) {
      showPlans('Sign in and pick a plan to unlock every safe rewrite and the counter-offer.');
      return;
    }
    try {
      const full = await cloud.unlock(d.scan_id);
      renderCloud({ ...full, benchmarks: d.benchmarks, signed_in: true }, lastCloud.text, full.name || full.title || 'Contract');
    } catch (e) {
      if (e && e.response && e.response.status === 402) showPlans('You’re out of full reviews. Pick a pack or go Pro to unlock this one.');
      else showPlans(errMsg(e));
    }
  }

  let billingChecked = false;
  async function billingInit() {
    const cloud = window.RedlineCloud;
    if (!cloud || !cloud.ready) return;
    const qs = new URLSearchParams(location.search);
    if (!billingChecked && cloud.user && qs.get('checkout') === 'success' && qs.get('session_id')) {
      billingChecked = true;
      try {
        const r = await cloud.confirm(qs.get('session_id'));
        showPlans(r.pro ? 'Welcome to Redline Pro. Every review is now unlocked.' : `Payment received. You have ${r.credits} full review${r.credits === 1 ? '' : 's'} to use.`);
      } catch (e) {
        showPlans(errMsg(e));
      }
      history.replaceState(null, '', location.pathname + '#pricing');
    }
    if (cloud.user && store.get('rl-buy')) {
      const pending = store.get('rl-buy');
      store.set('rl-buy', null);
      buy(pending);
      return;
    }
    try {
      const b = await cloud.billing();
      const status = document.getElementById('planStatus');
      if (b.paywall && cloud.user) {
        status.textContent = b.pro ? 'You’re on Redline Pro.' : `Full reviews left: ${b.credits}.`;
        status.hidden = false;
      } else {
        status.hidden = true;
      }
      document.getElementById('manageBilling').hidden = !b.can_manage;
    } catch {
      // Plans still work without the status line.
    }
  }
  addEventListener('redline:cloud', billingInit);
  billingInit();

  document.getElementById('manageBilling').addEventListener('click', async () => {
    try {
      const r = await window.RedlineCloud.portal();
      location.href = r.url;
    } catch (e) {
      showPlans(errMsg(e));
    }
  });

