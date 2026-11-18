"""网关侧代搜（server-side search）。

为什么需要这个模块
------------------
有些客户端的搜索工具根本不在本地执行，而是**转手调用外部 API**。典型是 DSH 的
`web_search`：它是内置插件 `dsh-web-search-deepseek`，把查询发到
`https://api.deepseek.com/anthropic/v1/messages`，靠 Anthropic 的
`web_search_20250305` 工具让 DeepSeek 服务端搜。所以——

    · 聊天链路走本网关 → AFS → DeepSeek 网页端，不需要 API Key，一切正常
    · 搜索链路却直连 api.deepseek.com，用的是 DSH 自己存的 Key
    · 那个 Key 一旦失效，搜索就固定返回 HTTP 401，模型重试几次后只能凭空作答

实测日志（2026-10-23 23:18）：
    Error: DeepSeek API error (HTTP 401): Authentication Fails, Your api key: ****78bN
    is invalid

这种情况在网关上无法通过改提示词解决——工具根本不经过网关。唯一稳的办法是
**网关自己搜**：模型想要 `web_search` 时，网关替它搜、替它读正文，再把结果
塞回对话让它作答。这也是「精简代理」本来就该有的兜底能力。

数据源
------
`cn.bing.com` 的 RSS 输出（`format=rss`）结构稳定、不用解析 JS、体积小；
失败时回退到 HTML 结果页的 `b_algo` 块。正文抓取只做「HTML → 文本」，
目标是把真实数字交给模型，不追求排版还原。
"""
import html as html_lib
import re
import xml.etree.ElementTree as ET

import requests

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')

_HEADERS = {
    'User-Agent': UA,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
}

# 有些站点（weather.com.cn 等）声明的 charset 是 ISO-8859-1，实际是 UTF-8。
# requests 会照着声明解码，直接得到乱码，所以统一按「探测结果」纠正一次。
_SUSPECT_CHARSETS = ('ISO-8859-1', 'latin-1', 'us-ascii', 'ascii')


def _decode(resp):
    enc = (resp.encoding or '').strip()
    if not enc or enc.upper() in [c.upper() for c in _SUSPECT_CHARSETS]:
        enc = resp.apparent_encoding or 'utf-8'
    try:
        return resp.content.decode(enc, 'replace')
    except (LookupError, TypeError):
        return resp.content.decode('utf-8', 'replace')


def _get(url, params=None, timeout=15):
    """GET 并返回文本；失败返回 ('', 原因)。"""
    try:
        r = requests.get(url, params=params, headers=_HEADERS, timeout=timeout,
                         allow_redirects=True)
    except requests.RequestException as e:
        return '', f'{type(e).__name__}: {e}'
    if r.status_code >= 400:
        return '', f'HTTP {r.status_code}'
    return _decode(r), ''


# ── 搜索源 ──────────────────────────────────────────────

def _bing_rss(query, count):
    """必应的 RSS 结果：<item> 里有 title/link/description，干净。"""
    text, err = _get('https://cn.bing.com/search',
                     {'q': query, 'format': 'rss', 'count': max(count, 10)})
    if not text:
        return [], err
    try:
        root = ET.fromstring(text.encode('utf-8', 'replace'))
    except ET.ParseError as e:
        return [], f'RSS 解析失败: {e}'
    out = []
    for item in root.iter('item'):
        title = (item.findtext('title') or '').strip()
        link = (item.findtext('link') or '').strip()
        desc = re.sub(r'<[^>]+>', '', item.findtext('description') or '')
        desc = html_lib.unescape(desc).strip()
        if title and link.startswith('http'):
            out.append({'title': title, 'url': link, 'snippet': desc})
        if len(out) >= count:
            break
    return out, '' if out else 'RSS 无结果'


def _bing_html(query, count):
    """回退：解析结果页里的 <li class="b_algo">。"""
    text, err = _get('https://cn.bing.com/search', {'q': query, 'setlang': 'zh-CN'})
    if not text:
        return [], err
    out = []
    for block in re.findall(r'<li class="b_algo".*?</li>', text, re.S)[:count]:
        m_t = re.search(r'<h2>.*?<a[^>]*>(.*?)</a>', block, re.S)
        m_u = re.search(r'<a[^>]+href="(https?://[^"]+)"', block)
        m_p = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)

        def clean(x):
            return html_lib.unescape(re.sub(r'<[^>]+>', '', x or '')).strip()

        title, url, snip = clean(m_t.group(1) if m_t else ''), (m_u.group(1) if m_u else ''), \
            clean(m_p.group(1) if m_p else '')
        if title and url:
            out.append({'title': title, 'url': url, 'snippet': snip})
    return out, '' if out else 'HTML 无结果'


def search(query, count=5):
    """多源尝试，返回结果列表（可能为空）。"""
    for fn in (_bing_rss, _bing_html):
        results, err = fn(query, count)
        if results:
            return results, fn.__name__
        if err:
            pass
    return [], 'all-failed'


# ── 查询改写 ────────────────────────────────────────────
#
# 实测必应（尤其 cn.bing.com）对**长查询**非常敏感：多带几个修饰词，
# 排名就从「中国天气网」掉到「百度百科 / 政府门户 / 旅游攻略」——
# 正是模型爱写的那种查询（"重庆今天天气 2026年10月23日, 重庆今日气温 天气现象 湿度"）。
# 而把时间词、日期、标点摘掉后的短查询（"重庆天气"）反而精准命中。
# 所以这里生成几个变体一起搜，再合并去重：用几个短查询换召回率。

_DATE_RE = re.compile(r'\b(19|20)\d{2}\s*[-/年.]\s*\d{1,2}\s*[-/月.]\s*\d{1,2}\s*日?')
_NOISE_WORDS = ('今天', '今日', '现在', '实时', '最新', '目前', '明天', '昨天',
                '如何', '怎么样', '怎样', '查询', '多少', '情况', '请', '帮我')


def query_variants(query, limit=4):
    """原查询 → 摘掉日期/时间词/标点的短查询 → 只留首个词。

    最后一步（"重庆今天天气 气温" → "重庆天气"）是关键：必应上带修饰词的
    长查询会把天气站挤到第 6 条之后，短查询反而第 1 条就是中国天气网。
    """
    out = []

    def add(q):
        q = re.sub(r'\s+', ' ', q).strip(' ，,。.;；')
        if q and q not in out:
            out.append(q)

    add(query)
    t = _DATE_RE.sub(' ', query)
    t = re.sub(r'[，,;；、|]+', ' ', t)
    for w in _NOISE_WORDS:  # 直接删词，不插空格——"重庆今天天气" → "重庆天气"
        t = t.replace(w, '')
    t = re.sub(r'\s+', ' ', t).strip()
    add(t)
    words = t.split()
    if len(words) > 1:
        add(words[0])
        add(' '.join(words[:2]))
    return out[:max(1, limit)]


# ── 正文抓取 ────────────────────────────────────────────

_BLOCK_TAGS = ('script', 'style', 'noscript', 'svg', 'head', 'iframe', 'form')


def _denoise(text):
    """去掉导航菜单噪声。

    通用正文抽取的第一个坑：网页前半页往往是导航（首页 / 预报 / 预警 / 热门城市 /
    北京 上海 成都 …），逐行占掉了字符预算，真正的数据被截到预算之外。
    这里只做两件安全的事：重复行去重（导航项会重复出现多次）、
    丢掉「≤3 字且不含数字」的孤立短词（正是菜单项的形状）。
    """
    seen = set()
    out = []
    for raw in (text or '').split('\n'):
        line = raw.strip()
        if not line or line in seen:
            continue
        seen.add(line)
        if len(line) <= 3 and not re.search(r'\d', line):
            continue
        out.append(line)
    return '\n'.join(out)


def _best_window(text, max_chars, seed_lines=12):
    """找出「信息密度最高」的位置作为起点，再从那里往后取满字符预算。

    为什么不能只截开头：资讯类页面（中国天气网、门户、行情页）开头几百字
    几乎全是导航——"首页/预报/预警/热门城市/北京 上海 成都…"，只截头部
    等于把真正带数字的表格挤出预算。也不能取「最长的一段」：那会把新闻列表
    当正文。可靠的是**密度**：按「数字 + ℃ + %」计分并扣掉导航型短行，
    取密度最高的 12 行当种子，然后连续向后取（保证表格/列表不被切碎）。
    """
    if len(text) <= max_chars:
        return text
    lines = text.split('\n')
    if len(lines) <= seed_lines:
        return text[:max_chars]

    def density(seg):
        body = '\n'.join(seg)
        if not body:
            return 0.0
        nav = sum(1 for ln in seg if len(ln) <= 6 and not re.search(r'\d', ln))
        score = (len(re.findall(r'\d', body)) + 10 * body.count('℃')
                 + 3 * body.count('%') - 1.5 * nav)
        return score / len(body)

    best_i, best_d = 0, -1.0
    for i in range(len(lines) - seed_lines + 1):
        d = density(lines[i:i + seed_lines])
        if d > best_d:
            best_d, best_i = d, i
    # 往回让几行：密度最高的那几行往往已经是表格中段，把表头（日期/城市）让掉了
    start = max(0, best_i - seed_lines // 2)
    return '\n'.join(lines[start:])[:max_chars]


def extract_text(page, max_chars=1200):
    """HTML → 可读文本。只做够用的清洗：去脚本样式、块级换行、压缩空白。"""
    if not page:
        return ''
    t = page
    for tag in _BLOCK_TAGS:
        t = re.sub(rf'(?is)<{tag}\b[^>]*>.*?</{tag}>', ' ', t)
    t = re.sub(r'(?is)<br\s*/?>', '\n', t)
    t = re.sub(r'(?is)</(p|div|li|tr|h[1-6]|section|article|td)>', '\n', t)
    t = re.sub(r'(?s)<[^>]+>', ' ', t)
    t = html_lib.unescape(t)
    t = t.replace('\u3000', ' ').replace('\xa0', ' ')
    t = re.sub(r'[ \t\r\f\v]+', ' ', t)
    t = re.sub(r'\n[ \t]*', '\n', t)
    t = _denoise(re.sub(r'\n{3,}', '\n\n', t))
    return _best_window(t, max_chars) if max_chars > 0 else t


def fetch_page(url, max_chars=1200, timeout=12):
    """抓一页并抽正文；失败返回 ('', 原因)。"""
    raw, err = _get(url, timeout=timeout)
    if not raw:
        return '', err
    body = extract_text(raw, max_chars)
    if not body:
        return '', '正文为空（可能是 JS 渲染页面）'
    # 带上 <title>：模型得知道这段正文属于哪个页面（哪个城市、哪家公司）
    m = re.search(r'(?is)<title[^>]*>(.*?)</title>', raw)
    if m:
        title = html_lib.unescape(re.sub(r'<[^>]+>', ' ', m.group(1)))
        title = re.sub(r'\s+', ' ', title).strip()[:80]
        if title:
            return f'《{title}》\n{body}', ''
    return body, ''


# ── 组装给模型看的文本块 ────────────────────────────────

def build_block(queries, cfg=None, fetch_tool='web_fetch'):
    """按查询搜索 + 抓正文，拼成一段可直接塞进提示的词。

    返回 '' 表示彻底没拿到东西（调用方应回退到「把工具调用交回客户端」）。
    """
    cfg = cfg or {}
    max_queries = int(cfg.get('server_search_max_queries', 2) or 2)
    max_results = int(cfg.get('server_search_max_results', 5) or 5)
    fetch_top = int(cfg.get('server_search_fetch_top', 2) or 0)
    page_chars = int(cfg.get('server_search_page_chars', 1200) or 0)

    queries = [q.strip() for q in (queries or []) if q and q.strip()][:max_queries]
    if not queries:
        return ''

    lines = ['[网关代搜结果]',
             '说明：客户端自带的搜索工具当前不可用（上游鉴权失败），'
             '以下内容由网关代你搜索并抓取了网页正文。']

    # 每个查询跑几个变体，再**轮流**取一条 —— 不能先把这个变体取满配额，
    # 否则「重庆天气」这类精准变体排在后面，永远轮不到它露脸。
    per_query = max(3, -(-max_results // max(1, len(queries))))  # ceil
    collected = []      # [(query, [result, …]), …]
    seen_urls = set()
    for q in queries:
        buckets = []
        for variant in query_variants(q):
            results, _provider = search(variant, per_query)
            buckets.append(results or [])
        hits = []
        for rank in range(per_query):
            for bucket in buckets:
                if len(hits) >= per_query:
                    break
                if rank < len(bucket):
                    r = bucket[rank]
                    if r['url'] in seen_urls:
                        continue
                    seen_urls.add(r['url'])
                    hits.append(r)
            if len(hits) >= per_query:
                break
        if hits:
            collected.append((q, hits))

    if not collected:
        return ''

    # 挑要抓正文的页面：百科/问答/门户首页这类基本没有目标数据，
    # 抓它们纯属浪费抓取预算和提示长度。
    fetch_skip = ('baike.baidu.com', 'zhihu.com', 'wenku.baidu', 'baike.sogou',
                  'zhidao.baidu', 'so.com/link', 'baijiahao.baidu')

    def _fetchworthy(url):
        low = url.lower()
        if any(bad in low for bad in fetch_skip):
            return False
        path = low.split('://', 1)[-1].split('/', 1)
        tail = path[1].split('?', 1)[0].split('#', 1)[0] if len(path) > 1 else ''
        return bool(tail.strip('/'))  # 站点首页（含 /?x=1 这种）不抓

    fetch_targets = [r['url'] for _q, hits in collected for r in hits
                     if _fetchworthy(r['url'])][:fetch_top]
    bodies = {}
    for url in fetch_targets:
        body, err = fetch_page(url, page_chars)
        bodies[url] = (body, err)

    seq = 0
    for q, hits in collected:
        lines.append(f'\n查询：{q}')
        for r in hits:
            seq += 1
            lines.append(f'\n{seq}. {r["title"]}\n   URL: {r["url"]}')
            if r.get('snippet'):
                lines.append(f'   摘要: {r["snippet"][:300]}')
            if r['url'] in bodies:
                body, err = bodies[r['url']]
                if body:
                    lines.append(f'   正文: {body}')
                else:
                    lines.append(f'   正文: 抓取失败（{err}）')

    lines.append(
        '\n[网关代搜结果结束]\n'
        '上面「正文」是真实抓取的网页内容，可直接据此作答；'
        f'如果还缺数据，可用 {fetch_tool} 打开列表里的其它 URL 补充。'
        '绝对不要编造数字——抓不到就如实说明未获取到。')
    return '\n'.join(lines)


if __name__ == '__main__':  # 手工烟测：python server_search.py 重庆天气
    import sys
    qs = sys.argv[1:] or ['重庆天气']
    print(build_block(qs, {}))
