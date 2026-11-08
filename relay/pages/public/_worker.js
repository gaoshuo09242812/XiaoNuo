// Fixed public ingress for the computer and device; original role authentication applies.
// Service binding preserves existing authentication, limits and Durable Object room.
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const headers = {'Cache-Control': 'no-store'};
    if (url.protocol !== 'https:') return new Response('TLS_REQUIRED', {status:426,headers});
    if (url.search || request.headers.has('Origin')) return new Response('RELAY_REQUEST_REJECTED', {status:403,headers});
    const hostReply = /^\/host\/[0-9a-f]{64}\/reply\/[0-9a-f-]{36}$/.test(url.pathname);
    const deviceReply = /^\/reply\/[0-9a-f]{64}\/[0-9a-f-]{36}$/.test(url.pathname);
    const websocket = ['/healthz','/api/v1/device-link/ws'].includes(url.pathname) || /^\/host\/[0-9a-f]{64}$/.test(url.pathname);
    if (!websocket && !hostReply && !deviceReply) return new Response('NOT_FOUND', {status:404,headers});
    if (websocket && request.method !== 'GET') return new Response('METHOD_NOT_ALLOWED', {status:405,headers});
    if (hostReply && request.method !== 'POST') return new Response('METHOD_NOT_ALLOWED', {status:405,headers});
    if (deviceReply && !['GET','HEAD'].includes(request.method)) return new Response('METHOD_NOT_ALLOWED', {status:405,headers});
    return env.RELAY_SERVICE.fetch(request);
  }
};
