"""Merchant-facing web pages for login and device verification.

Deliberately plain: a non-technical shop owner sees only Chinese labels, a device
name and two buttons. No API key, token, provider or session wording appears
anywhere in this module.

Templates use ``{{name}}`` placeholders replaced via :func:`str.replace` rather
than f-strings, because the embedded JavaScript is full of braces.
"""
from __future__ import annotations

import html

_STYLE = """
:root{color-scheme:light}
body{font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif;background:#f4f6f8;margin:0;
padding:32px 16px;color:#1f2933}
.card{max-width:460px;margin:0 auto;background:#fff;border-radius:12px;padding:28px 24px;
box-shadow:0 2px 12px rgba(16,24,40,.08)}
h1{font-size:20px;margin:0 0 6px}
h1+p{margin-top:0}
p{line-height:1.7;color:#52606d;margin:8px 0}
label{display:block;font-size:14px;color:#3e4c59;margin:14px 0 6px}
input{width:100%;box-sizing:border-box;padding:11px 12px;font-size:15px;border:1px solid #cbd2d9;
border-radius:8px;background:#fff}
input:focus{outline:2px solid #2563eb;border-color:#2563eb}
button{margin-top:18px;width:100%;padding:12px;font-size:16px;border:0;border-radius:8px;cursor:pointer}
.primary{background:#2563eb;color:#fff}
.secondary{background:#e4e7eb;color:#1f2933;margin-top:10px}
.code{font-size:30px;letter-spacing:4px;font-weight:700;text-align:center;background:#eef2ff;
border-radius:8px;padding:14px;margin:10px 0}
.device{background:#f0f4f8;border-radius:8px;padding:14px;margin:12px 0;text-align:center}
.hint{font-size:13px;color:#7b8794}
.msg{margin-top:14px;padding:11px;border-radius:8px;font-size:14px;display:none}
.err{background:#fdeaea;color:#a11}
.ok{background:#e7f6ec;color:#14653a}
ul{padding-left:20px;color:#52606d;font-size:14px;line-height:1.8}
"""

_SCRIPT = """
async function post(url, body, csrf){
  const headers={'Content-Type':'application/json'};
  if(csrf) headers['X-CSRF-Token']=csrf;
  const r=await fetch(url,{method:'POST',headers:headers,body:JSON.stringify(body||{})});
  let data={};
  try{data=await r.json();}catch(e){}
  return {ok:r.ok,status:r.status,data:data};
}
function show(el,text,bad){
  el.textContent=text; el.className='msg '+(bad?'err':'ok'); el.style.display='block';
}
"""


def _fill(template: str, **values: str) -> str:
    for key, value in values.items():
        template = template.replace("{{%s}}" % key, str(value))
    return template


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>" + html.escape(title) + "</title><style>" + _STYLE + "</style></head><body>"
        '<div class="card">' + body + "</div>"
        "<script>" + _SCRIPT + "</script></body></html>"
    )


def _js_literal(value: str) -> str:
    """Embed a value as a JavaScript string literal.

    必须带两侧的双引号：之前只转义不加引号，吐出来是 ``csrf=abc123;``，
    浏览器整段 script 直接语法错误，改密码/确认设备的按钮全死 ——
    点了没反应的那种坏法，连报错都没有。
    """
    return '"' + html.escape(json_escape(str(value)), quote=True) + '"'


def json_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("</", "<\\/")


LOGIN_PAGE_HTML = _page(
    "商家登录",
    """
<h1>商家登录</h1>
<p>请输入运营人员提供的账号和密码。</p>
<label for="u">账号</label><input id="u" autocomplete="username" autofocus>
<label for="p">密码</label><input id="p" type="password" autocomplete="current-password">
<button class="primary" id="go">登录</button>
<div class="msg" id="m"></div>
<p class="hint">如果这是您第一次登录，登录后系统会引导您修改初始密码。</p>
<script>
const m=document.getElementById('m');
document.getElementById('go').onclick=async function(){
  m.style.display='none';
  const r=await post('/v1/web/login',{username:document.getElementById('u').value,
    password:document.getElementById('p').value});
  if(!r.ok){show(m,r.data.message||'账号或密码不正确',true);return;}
  location.href='/device';
};
</script>
""",
)


def render_first_login(merchant: str, csrf: str = "") -> str:
    return _page(
        "首次登录",
        _fill(
            """
<h1>欢迎，{{merchant}}</h1>
<p>为了保护您的账号安全，请先修改初始密码。</p>
<label for="cur">当前密码</label><input id="cur" type="password" autocomplete="current-password">
<label for="nw">新密码（至少 8 位）</label><input id="nw" type="password" autocomplete="new-password">
<button class="primary" id="go">修改密码</button>
<div class="msg" id="m"></div>
<p class="hint">修改完成后，请回到电脑上的软件重新登录。</p>
<script>
const m=document.getElementById('m'), csrf={{csrf}};
document.getElementById('go').onclick=async function(){
  m.style.display='none';
  const r=await post('/v1/web/password/change',{current_password:document.getElementById('cur').value,
    new_password:document.getElementById('nw').value},csrf);
  if(!r.ok){show(m,r.data.message||'修改失败',true);return;}
  show(m,r.data.message||'密码已修改',false);
};
</script>
""",
            merchant=html.escape(merchant),
            csrf=_js_literal(csrf),
        ),
    )


def render_device_entry(merchant: str) -> str:
    return _page(
        "设备验证",
        _fill(
            """
<h1>设备验证</h1>
<p>商家：<strong>{{merchant}}</strong></p>
<p>请在电脑上的软件中点击“登录”，然后把软件上显示的验证码输入到下面的输入框。</p>
<label for="code">验证码</label>
<input id="code" placeholder="例如：BCDF-2345" maxlength="9" style="text-transform:uppercase">
<button class="primary" id="go">下一步</button>
<div class="msg" id="m"></div>
<script>
document.getElementById('go').onclick=function(){
  location.href='/device?code='+encodeURIComponent(document.getElementById('code').value.trim().toUpperCase());
};
</script>
""",
            merchant=html.escape(merchant),
        ),
    )


def render_device_confirm(merchant: str, user_code: str, device_name: str, csrf: str) -> str:
    return _page(
        "确认设备",
        _fill(
            """
<h1>确认这台电脑</h1>
<p>商家：<strong>{{merchant}}</strong></p>
<div class="device">这台电脑的名称是<br><strong>{{device}}</strong></div>
<p>请确认这是您自己的电脑。确认后，这台电脑才可以登录使用。</p>
<div class="code">{{code}}</div>
<button class="primary" id="ok">确认是这台电脑</button>
<button class="secondary" id="no">不是，取消</button>
<div class="msg" id="m"></div>
<script>
const m=document.getElementById('m'), code={{code}}, csrf={{csrf}};
document.getElementById('ok').onclick=async function(){
  m.style.display='none';
  const r=await post('/v1/web/device/authorize',{user_code:code,action:'approve'},csrf);
  if(!r.ok){show(m,r.data.message||'确认失败，请重新登录软件',true);return;}
  show(m,'已确认，请回到电脑上的软件继续。',false);
};
document.getElementById('no').onclick=async function(){
  m.style.display='none';
  const r=await post('/v1/web/device/authorize',{user_code:code,action:'deny'},csrf);
  show(m,r.ok?'已取消。':'取消失败，请重试。',!r.ok);
};
</script>
""",
            merchant=html.escape(merchant),
            device=html.escape(device_name),
            code=_js_literal(user_code),
            csrf=_js_literal(csrf),
        ),
    )


def render_device_page(merchant: str, user_code: str, pending: object, csrf: str = "") -> str:
    if pending is not None:
        # The gateway passes a plain dict row, not an attribute object.
        device_name = pending.get("device_name", "") if isinstance(pending, dict) else getattr(pending, "device_name", "")
        return render_device_confirm(merchant, user_code, str(device_name or ""), csrf)
    return render_device_entry(merchant)


SETTINGS_PAGE_HTML = _page(
    "设置说明",
    """
<h1>云端解析会发送哪些信息</h1>
<p>登录之后，只有在您主动要求解析某条群消息时，软件才会把<strong>本次需要解析的内容</strong>发送到云端，包括：</p>
<ul>
  <li>这一条待解析的消息内容和发送时间</li>
  <li>已确认的客户身份（姓名、群昵称）</li>
  <li>商品资料和单位规则</li>
  <li>该客户在该消息时间之前的近期订单和已确认的习惯信息</li>
  <li>该订单当前的状态</li>
</ul>
<p><strong>不会</strong>发送：您的微信聊天全库、微信登录状态、其他客户的数据。</p>
<h1>数据保留</h1>
<ul>
  <li>完整消息、订单和审核记录保存在本机，长期保留，断网也能查看。</li>
  <li>云端在解析完成后立即删除提交的消息正文，只保留解析结论。</li>
  <li>云端解析使用独立临时会话，任务结束即清理，不作为您的长期记忆。</li>
  <li>云端停用账号后，本地已保存的订单不受影响。</li>
</ul>
<p class="hint">套餐到期或网络不通时，本地订单查看、人工修改和导出仍然可以正常使用。</p>
""",
)


def login_redirect_page() -> str:
    return _page("请先登录", "<h1>请先登录</h1><p>请先回到电脑上的软件，点击“登录”。</p>")


#: Shown for /device and /settings when the browser has no product session yet.
page_shell = login_redirect_page


class _DevicePage:
    """Template object so the gateway can render the device page lazily."""

    def render(self, merchant: str = "", user_code: str = "", pending: object = None, csrf: str = "") -> str:
        return render_device_page(merchant, user_code, pending, csrf)


DEVICE_PAGE_HTML = _DevicePage()
