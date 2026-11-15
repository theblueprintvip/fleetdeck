// Connects the page to Redline's backend: sign-in, AI review, counter-offers, saved contracts.
import { api, auth } from '@appdeploy/client';

const cloud = {
  ready: false,
  user: null,
  signIn: () => auth.signInWithRedirect(),
  async signOut() {
    await auth.signOut();
    cloud.user = null;
    emit();
  },
  analyze: payload => api.post('/api/analyze', payload).then(r => r.data),
  counterOffer: payload => api.post('/api/counter-offer', payload).then(r => r.data),
  scans: () => api.get('/api/scans').then(r => r.data),
  wallet: () => api.get('/api/wallet').then(r => r.data),
  deleteScan: id => api.delete('/api/scans/' + encodeURIComponent(id)).then(r => r.data),
  me: () => api.get('/api/me').then(r => r.data),
  saveMe: profile => api.put('/api/me', profile).then(r => r.data),
};

function emit() {
  window.dispatchEvent(new CustomEvent('redline:cloud', { detail: cloud }));
}

window.RedlineCloud = cloud;

auth.getUser()
  .then(user => { cloud.user = user; })
  .catch(() => { cloud.user = null; })
  .finally(() => {
    cloud.ready = true;
    emit();
  });
