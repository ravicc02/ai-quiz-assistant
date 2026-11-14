from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit


PAGE_PROBE = r"""(...args) => {
  const focus = JSON.parse(args[0] || '[]');
  const roots = focus.length ? [...document.querySelectorAll('[data-mumu-unit]')].filter(node => focus.includes(node.getAttribute('data-mumu-unit')))
    : [...document.querySelectorAll('.questionLi[id^="question"],.TiMu')];
  const attrs = new Set(['id','class','name','type','role','data','qid','qtype','questionid','itemid','tabindex','contenteditable','aria-checked','aria-pressed','aria-label','data-mumu-unit','data-mumu-kind','data-mumu-hidden-id','data-mumu-option','data-mumu-field','data-mumu-iframe']);
  const forbidden = new Set(['SCRIPT','STYLE','LINK','META','NOSCRIPT','OBJECT','EMBED','SVG']);
  let count = 0, truncated = false;
  function tree(node, depth = 0) {
    if (count++ > 5000 || depth > 16) { truncated = true; return null; }
    if (node.nodeType === Node.TEXT_NODE) { const text = node.textContent.trim(); return text ? {tag:'#text',text:text.slice(0,2000)} : null; }
    if (node.nodeType !== Node.ELEMENT_NODE || forbidden.has(node.tagName)) return null;
    const attributes = Object.fromEntries([...node.attributes].filter(item => attrs.has(item.name)).map(item => [item.name,item.value.slice(0,500)]));
    const result = {tag:node.tagName.toLowerCase(),attributes};
    if (node.matches('.read_type,.colorShallow')) result.type_label = node.textContent.trim().slice(0,80);
    const handler = node.getAttribute('onclick') || '';
    if (handler) result.click_handler = handler.match(/^\s*([\w.]+)/)?.[1] || '[unknown]';
    if (node.matches('input,textarea,select')) {
      result.control = {checked:Boolean(node.checked),disabled:node.disabled,read_only:node.readOnly};
      const sensitive = /password|user|account|phone|email|token|enc|secret|cookie/i.test(node.name + ' ' + node.id + ' ' + node.type);
      if (!sensitive) result.value = String(node.value || '').slice(0,12000);
    }
    if (node.tagName === 'IFRAME') {
      result.frame_url = node.src;
      try { result.editor_text = node.contentDocument?.body?.innerText?.slice(0,12000) || ''; }
      catch(error) { result.access_error = error.name; }
    }
    result.children = [...node.childNodes].slice(0,100).map(child => tree(child,depth+1)).filter(Boolean);
    return result;
  }
  const selected = roots.length ? roots.slice(0,60) : [document.body];
  return JSON.stringify({
    page:{url:location.href,title:document.title,ready_state:document.readyState,viewport:{width:innerWidth,height:innerHeight,dpr:devicePixelRatio},
      user_agent:navigator.userAgent,language:navigator.language,visibility:document.visibilityState},
    frames:[...document.querySelectorAll('iframe')].slice(0,40).map(node=>({id:node.id,name:node.name,src:node.src,visible:!!node.getClientRects().length})),
    root_count:roots.length,focus_unit_ids:focus,dom:selected.map(node=>tree(node)).filter(Boolean),truncated,
  });
}"""


def useful_request_failure(url, reason, resource_type=''):
    failure = str(reason or '')
    parsed = urlsplit(str(url or ''))
    if 'net::ERR_ABORTED' in failure and str(resource_type or '') != 'document':
        return False
    if (
        'net::ERR_BLOCKED_BY_ORB' in failure
        and parsed.hostname == 'robot-lc.chaoxing.com'
    ):
        return False
    return True


def useful_http_failure(url, status, resource_type=''):
    parsed = urlsplit(str(url or ''))
    if (
        int(status or 0) == 404
        and str(resource_type or '') == 'stylesheet'
        and parsed.hostname
        and parsed.hostname.endswith('chaoxing.com')
        and '/spacx/new/css/work-css-tpl.cssx' in parsed.path
    ):
        return False
    return int(status or 0) >= 400


async def capture_page(trace, page, name, focus=()):
    if not trace.available: return
    try:
        raw = await asyncio.wait_for(page.evaluate(PAGE_PROBE, json.dumps(list(focus))), 4)
        data = json.loads(raw) if isinstance(raw, str) else raw
        trace.snapshot(name, data)
    except Exception as error:
        trace.record("diagnostics.capture_failed", str(error), {"snapshot": name, "exception_type": type(error).__name__}, component="diagnostics", level="warning")


def attach_network(trace, context):
    """Capture response metadata only: never headers, bodies or request credentials."""
    if not trace.available: return
    counts = {"network": 0}
    def response_received(response):
        if (
            counts["network"] >= 100
            or not useful_http_failure(
                response.url,
                response.status,
                response.request.resource_type,
            )
        ):
            return
        counts["network"] += 1
        trace.record("browser.http_error", data={"url": response.url, "status": response.status,
            "method": response.request.method, "resource_type": response.request.resource_type}, component="browser", level="warning")
    def request_failed(request):
        if (
            counts["network"] >= 100
            or not useful_request_failure(
                request.url,
                request.failure,
                request.resource_type,
            )
        ):
            return
        counts["network"] += 1
        trace.record("browser.request_failed", data={
            "url": request.url,
            "method": request.method,
            "reason": request.failure,
            "resource_type": request.resource_type,
        }, component="browser", level="warning")
    def page_opened(page):
        page.on("pageerror", lambda error: trace.record("browser.page_error", str(error), component="browser", level="warning"))
    context.on("response", response_received)
    context.on("requestfailed", request_failed)
    context.on("page", page_opened)
    for page in context.pages: page_opened(page)
