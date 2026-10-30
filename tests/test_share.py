"""v1.1.5 分享版測試：設定檔、分享頁內容、安全檢查、scan --share、公開／下架流程（gh 全部用假的）、run.bat。

不連網、不執行真的 gh、不碰使用者的 data／cache／結果。執行：python -m unittest discover -s tests -v
"""
import argparse, base64, copy, json, math, os, re, subprocess, sys, tempfile, unittest
from unittest import mock

import test_usability as tu          # 注意：不要用 from ... import 把測試類別拉進來，否則會被重複執行

screener, ROOT = tu.screener, tu.ROOT
import share  # noqa: E402

LOGIN = 'yourname'
REPO = 'yourname/tw-stock-screener-results'
FAKE_GH = 'C:\\fake\\gh.exe'


def make_page(date='2026-10-22', capital=1000000, tweak=None):
    res = tu.fake_results(capital)
    for r in res:
        if 'date' in r:
            r['date'] = date
    if tweak:
        tweak(res)
    return screener.render_share_html(res)


def ns(**k):
    d = dict(data=None, config=None, list=None, capital=None, risk=None, out=None, json=None, txt=None, html=None,
             csv=None, out_dir=None, open=False, share=False)
    d.update(k)
    return argparse.Namespace(**d)


class Chdir:
    """在暫存資料夾裡執行（分享版檢查會看目前所在的資料夾有沒有 自選股.csv，不能讓測試讀到使用者真的那份）"""

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.old = os.getcwd()
        os.chdir(self.path)

    def __exit__(self, *a):
        os.chdir(self.old)


# ---------- 設定檔 ----------
class TestShareConfig(tu.TempCase):
    def cfg(self, text):
        return tu.write(os.path.join(self.tmp, '設定.txt'), text)

    def test_parse_onoff(self):
        for v in ('1', 'y', 'YES', 'True', 'on', '是', '開', ' "1" '):
            self.assertTrue(screener.parse_onoff(v), v)
        for v in ('', '0', 'n', 'No', 'false', 'OFF', '否', '關'):
            self.assertFalse(screener.parse_onoff(v), v)
        for v in ('2', 'maybe', 'yes!'):
            with self.assertRaises(ValueError, msg=v) as cm:
                screener.parse_onoff(v)
            self.assertIn('只能寫 1（開）或 0（關）', str(cm.exception))

    def test_parse_repo(self):
        for v, want in (('', None), ('yourname/results', 'yourname/results'), (' "a-b/x.y_z-1" ', 'a-b/x.y_z-1'),
                        ('A1/b', 'A1/b'), ('a' * 39 + '/x', 'a' * 39 + '/x')):
            self.assertEqual(screener.parse_repo(v), want, v)
        for v in ('yourname', '/x', 'x/', 'a b/c', '-a/b', 'a-/b', 'a--b/c', 'a/b/c', 'a/.', 'a/..', 'a/b.git',
                  'a/B.GIT', 'a' * 40 + '/x', '你好/x', 'a/b c', 'https://github.com/a/b'):
            with self.assertRaises(ValueError, msg=v) as cm:
                screener.parse_repo(v)
            self.assertIn('帳號/儲存庫名稱', str(cm.exception))

    def test_load_config_defaults(self):
        c, err = tu.quiet(screener.load_config, self.cfg('capital=100\n'))
        self.assertEqual((c['share'], c['share_publish'], c['share_repo']), (False, False, None))
        self.assertEqual(err, '')

    def test_load_config_values_and_no_unknown_warning(self):
        c, err = tu.quiet(screener.load_config, self.cfg('share=1\nshare_publish = 開\nshare_repo = yourname/res  # x\n'))
        self.assertEqual((c['share'], c['share_publish'], c['share_repo']), (True, True, 'yourname/res'))
        self.assertEqual(err, '')

    def test_empty_values_keep_defaults(self):
        c, _ = tu.quiet(screener.load_config, self.cfg('share=\nshare_publish=\nshare_repo=\n'))
        self.assertEqual((c['share'], c['share_publish'], c['share_repo']), (False, False, None))

    def test_bad_values_report_line(self):
        for text, hint in (('capital=1\nshare=maybe\n', '只能寫 1'), ('share_publish=2\n', '只能寫 1'),
                           ('share_repo=not a repo\n', '帳號/儲存庫名稱')):
            with self.assertRaises(screener.UserError) as cm:
                screener.load_config(self.cfg(text))
            self.assertEqual(cm.exception.code, 9)
            self.assertIn('行', cm.exception.cause)
            self.assertIn(hint, cm.exception.cause)

    def test_describe_config(self):
        c, _ = tu.quiet(screener.load_config, self.cfg('capital=1000000\n'))
        self.assertEqual(len(screener.describe_config(c)), 4)       # 預設的輸出和以前一樣
        c, _ = tu.quiet(screener.load_config, self.cfg('share=1\n'))
        d = screener.describe_config(c)
        self.assertEqual(len(d), 5)
        self.assertIn('分享版', d[4])
        c, _ = tu.quiet(screener.load_config, self.cfg('share_publish=1\nshare_repo=yourname/res\n'))
        d = screener.describe_config(c)
        self.assertEqual(len(d), 6)
        self.assertIn('yourname/res', d[5])

    def test_quiet_params(self):
        p = tu.write(os.path.join(self.tmp, 'l.csv'), '代號,名稱\n2330,台積電\n'.encode('cp950'))
        wl, err = tu.quiet(screener.load_list, p, True)
        self.assertEqual((wl, err), ({'2330': '台積電'}, ''))
        self.assertEqual(tu.quiet(screener.read_text, p, 'x', True)[1], '')
        self.assertIn('Big5', tu.quiet(screener.load_list, p)[1])

    def test_template_still_loads_and_has_share_section(self):
        c, err = tu.quiet(screener.load_config, screener.TEMPLATE)
        self.assertEqual((c['share'], c['share_publish'], c['share_repo']), (False, False, None))
        self.assertEqual(err, '')
        t = tu.readb(screener.TEMPLATE).decode('utf-8')
        for s in ('share=0', 'share_publish=0', 'share_repo=', '--takedown', 'gh auth login', 'yourname/'):
            self.assertIn(s, t)

    def test_write_config_keeps_share_lines(self):
        p = self.cfg('capital=1\nshare=1\nshare_publish=0\nshare_repo=yourname/res\n')
        screener.write_config(p, 5000000, 0.03, 'full')
        c, _ = tu.quiet(screener.load_config, p)
        self.assertEqual((c['capital'], c['share'], c['share_repo']), (5000000, True, 'yourname/res'))


# ---------- 分享頁內容 ----------
class Trip(dict):
    """分享頁不該碰的欄位一被讀取就丟錯"""
    BANNED = {'size', 'notes', 'watch', 'stages', 'week', 'osc'}

    def __getitem__(self, k):
        if k in self.BANNED:
            raise AssertionError('分享版讀了不該讀的欄位：' + k)
        return super().__getitem__(k)

    def get(self, k, d=None):
        if k in self.BANNED:
            raise AssertionError('分享版讀了不該讀的欄位：' + k)
        return super().get(k, d)

    def __contains__(self, k):
        if k in self.BANNED:
            raise AssertionError('分享版讀了不該讀的欄位：' + k)
        return super().__contains__(k)


class TestShareRender(unittest.TestCase):
    def test_tripwire_and_content(self):
        res = [Trip(r) for r in tu.fake_results(1000000)]
        page = screener.render_share_html(res)
        for s in ('候選標的（4 檔）', '台泥', '台積電', '元大S&amp;P500', '2026-10-22（星期四）',
                  '收盤資料｜全市場掃描 7 檔（資料有效 6 檔）', '僅供研究，非投資建議', 'name="robots" content="noindex, nofollow"',
                  '關卡說明', '怎麼看這一頁', 'placeholder="輸入代號或名稱"', '分享版：只列出今天的候選標的與關卡說明',
                  '資料日期 2026-10-22｜僅供研究，非投資建議。', '<dt>突破型</dt><dd>1<small>檔</small>', 'id="cands"',
                  'id="help"', 'class="pb"', 'lang="zh-Hant"', 'charset="utf-8"'):
            self.assertIn(s, page, s)
        for s in ('部位', '總資金', '風險', '自選股', '觀察名單', '試算', '1,000,000', '1000000', '產生時間', 'stage-data',
                  'id="watch"', 'id="stages"', 'data-p0', '聯發科', '2454', '1216', '統一', '9999', '新股', '資料只有',
                  'https://', 'http://', '例如 2330 或 台積', 'application/json', '<noscript'):
            self.assertNotIn(s, page, s)

    def test_one_script_and_deterministic(self):
        a = make_page()
        self.assertEqual(a.count('<script'), 2)
        self.assertEqual(a.count('</script>'), 2)
        self.assertEqual(a[:a.index('</head>')].count('<script'), 1)
        self.assertEqual(a, make_page())
        self.assertEqual(a.encode('utf-8'), make_page().encode('utf-8'))
        self.assertEqual(a, make_page(capital=None))          # 總資金不影響分享頁的任何一個位元組
        self.assertEqual(a, make_page(capital=7654321))

    def test_help_text_rules(self):
        page = make_page()
        i = page.index('id="help"')
        text = re.sub(r'<[^>]+>', ' ', page[i:page.index('</section>', i)])
        self.assertIsNone(re.search(r'[0-9]{4,}', text))
        self.assertNotRegex(text, r'https?:')
        for w in share.FORBIDDEN_WORDS:
            self.assertNotIn(w, text)
        self.assertIn('近', text)

    def test_share_js(self):
        js = screener.SHARE_JS
        self.assertTrue(js.rstrip().endswith('})();'))
        self.assertNotIn('stage-data', js)
        self.assertNotIn('getElementById(\'wt\')', js)
        self.assertIn("getElementById('ct')", js)
        self.assertFalse([x for x in js.split('\n') if x.lstrip().startswith('//')])

    def test_escaping_hostile_names(self):
        def tweak(res):
            res[0]['name'] = '<img src=x onerror=alert(1)>"\'&'
            res[0]['setup'] = '</script><script>alert(2)</script>'
            res[0]['stop_why'] = '"><b>x</b>'
        page = make_page(tweak=tweak)
        self.assertNotIn('<img', page)
        self.assertNotIn('alert(2)</script>', page)
        self.assertEqual(page.count('<script'), 2)
        self.assertIn('&lt;img', page)

    def test_many_candidates_under_limit(self):
        res = [tu.fake_rec('%04d' % (1000 + i), '測試股票%d' % i, 100 + i * 0.5, 1.5, 2.0, '突破型' if i % 2 else '回測型',
                           95 + i * 0.5) for i in range(150)]
        for r in res:
            r['ma'] = dict(ma5=1.0, ma20=98 + 0.5, ma60=1.0, boll_up=120.0, boll_lo=90.0)
        page = screener.render_share_html(res).encode('utf-8')
        self.assertLess(len(page), share.MAX_BYTES)
        self.assertEqual(page.count(b'class="cand"'), 150)

    def test_full_render_unchanged(self):
        full = screener.render_html(tu.fake_results(), False, tu.meta_for())
        self.assertEqual(full.count('<script'), 3)
        self.assertIn('placeholder="例如 2330 或 台積"', full)
        self.assertIn('id="watch"', full)
        self.assertIn('產生時間', full)
        self.assertNotIn('noindex', full)

    def test_candidates_only_and_sorted(self):
        rows = screener.share_rows(tu.fake_results())
        self.assertEqual([r['code'] for r in rows], ['6874', '00646', '2330', '1101'])
        self.assertEqual([(r['type'], -r['vol_ratio']) for r in rows], sorted((r['type'], -r['vol_ratio']) for r in rows))
        self.assertEqual(len(rows), 4)
        for r in rows:
            self.assertLessEqual(set(r), set(screener.SHARE_FIELDS) | {'ma'})

    def test_does_not_modify_input(self):
        res = tu.fake_results()
        before = copy.deepcopy(res)
        screener.render_share_html(res)
        self.assertEqual(res, before)

    def test_bad_data_raises_value_error(self):
        def bad(fn, msg):
            res = tu.fake_results()
            fn(res)
            with self.assertRaises(ValueError, msg=msg):
                screener.render_share_html(res)
        bad(lambda r: r[0].update(close=float('nan')), 'nan')
        bad(lambda r: r[0].update(chg=float('inf')), 'inf')
        bad(lambda r: r[0].update(close=True), 'bool')
        bad(lambda r: r[0].update(stop='117'), 'str stop')
        bad(lambda r: r[0].update(vol_ratio=None), 'None')
        bad(lambda r: r[0].update(close=0), 'zero close')
        bad(lambda r: r[0].update(code='ABC'), 'code')
        bad(lambda r: r[0].update(code='1101 '), 'code space')
        bad(lambda r: r[0].update(code='12345678'), 'code long')
        bad(lambda r: r[0].update(type='其他'), 'type')
        bad(lambda r: r[0].update(name='x' * 61), 'name long')
        bad(lambda r: r[0].update(name='a\nb'), 'name ctrl')
        bad(lambda r: r[0].update(name=5), 'name int')
        bad(lambda r: r[0].update(setup='x' * 301), 'setup long')
        bad(lambda r: r[0].update(stop_why='x\x00'), 'stop_why ctrl')
        bad(lambda r: r[0].pop('stop'), 'missing field')
        bad(lambda r: r[0].update(ma=dict(ma20='x')), 'ma str')
        bad(lambda r: r[0].update(date='22/10/2026'), 'date')
        bad(lambda r: r[0].update(date='2026-13-45'), 'date invalid')
        with self.assertRaises(ValueError):
            screener.render_share_html([])
        with self.assertRaises(ValueError):
            screener.render_share_html([dict(code='9999', name='x', error='e')])

    def test_zero_candidates_page_is_valid(self):
        res = [r for r in tu.fake_results() if not r.get('type')]
        page = screener.render_share_html(res)
        self.assertIn('今天沒有標的同時通過所有關卡', page)
        rules = share.make_rules(capital=1000000)
        self.assertEqual(share.check_html(page.encode('utf-8'), rules), [])

    def test_topnav_factored_out(self):
        self.assertIn('資料日期 2026-10-22（星期四）', screener._topnav('2026-10-22'))


# ---------- 安全檢查 ----------
class TestLeakCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = make_page()
        cls.wl = {'2317': '鴻海', '2610': '華航', '3036': '文曄'}
        cls.paths = [r'E:\claude\tw-stock-screener', r'C:\Users\Someone', r'C:\work\secret folder\data']

    def rules(self, **k):
        d = dict(capital=1000000, amounts=[50000, 1234567], paths=self.paths, watchlist=self.wl,
                 repo='someowner/my-results')
        d.update(k)
        return share.make_rules(**d)

    def inject(self, text, where='<main>'):
        return self.page.replace(where, where + text, 1).encode('utf-8')

    def test_clean_passes(self):
        self.assertEqual(share.check_html(self.page.encode('utf-8'), self.rules()), [])

    def test_each_leak_is_blocked_without_echoing_it(self):
        cases = {
            'path win': r'C:\Users\Someone\proj\data',
            'path repo': r'E:\claude\tw-stock-screener\data\2330.csv',
            'path slash': 'E:/claude/tw-stock-screener/結果',
            'path tail': r'\claude\tw-stock-screener',
            'path lower': r'e:\CLAUDE\tw-stock-screener',
            'path custom': r'C:\work\secret folder\data',
            'path generic drive': r'D:\anything\here',
            'path unc': r'\\fileserver\share\x',
            'path home': '/home/bobby/project',
            'capital plain': '1000000',
            'capital comma': '1,000,000',
            'capital float': '1000000.0',
            'capital wan': '100萬',
            'amount plain': '1234567',
            'amount comma': '50,000',
            'thousands': '2,385',
            'watch code': '2317',
            'watch name': '鴻海',
            'email': 'bobby.tester@example.com',
            'capital key': 'capital=5',
            'risk key': 'risk = 3%',
            'share_repo key': 'share_repo=a/b',
            'forbidden word': '部位',
            'repo': 'someowner/my-results',
            'owner': 'someowner',
            'url': 'https://evil.example/x',
            'url no scheme': '//evil.example/x',
            'js url': 'javascript:alert(1)',
            'data url': 'data:text/html,xx',
            'import': '@import "x.css"',
            'css url': 'url(x.png)',
            'entity-escaped word': '&#37096;&#20301;',
        }
        rules = self.rules()
        for name, secret in cases.items():
            with self.subTest(name):
                probs = share.check_html(self.inject('<p>%s</p>' % secret).replace(b'<p>&amp;', b'<p>&'), rules)
                self.assertTrue(probs, name)
                joined = '\n'.join(probs)
                for part in (secret, secret.replace('\\', '/')):
                    if len(part) >= 5 and not part.startswith('&#'):
                        self.assertNotIn(part, joined, name)

    def test_structure_violations(self):
        rules = self.rules()
        p = self.page
        cases = {
            'second script': p.replace('</body>', '<script>var a=1;</script></body>'),
            'script attr': p.replace('<script>', '<script async>', 1),
            'img': p.replace('<main>', '<main><img src="x">'),
            'iframe': p.replace('<main>', '<main><iframe></iframe>'),
            'form': p.replace('<main>', '<main><form action="x"></form>'),
            'id watch': p.replace('<main>', '<main><section id="watch"></section>'),
            'id unknown': p.replace('id="top"', 'id="zzz"', 1),
            'data-p0': p.replace('<tbody class="cand" ', '<tbody class="cand" data-p0="5" ', 1),
            'data-code bad': p.replace('data-code="1101"', 'data-code="C:/x"', 1),
            'comment': p.replace('<main>', '<main><!-- note -->'),
            'processing instruction': p.replace('<main>', '<main><?php x ?>'),
            'onclick': p.replace('<main>', '<main><p onclick="x()">a</p>'),
            'href external': p.replace('href="#cands"', 'href="https://x.example"', 1),
            'href path': p.replace('href="#cands"', 'href="/etc/passwd"', 1),
            'style bad': p.replace('style="gap:8px"', 'style="background:red"', 1),
            'input text': p.replace('type="search"', 'type="text"', 1),
            'input password': p.replace('<main>', '<main><input type="password">'),
            'button submit': p.replace('type="button"', 'type="submit"', 1),
            'meta refresh': p.replace('<meta charset="utf-8">', '<meta charset="utf-8"><meta http-equiv="refresh" content="0;x">'),
            'link tag': p.replace('<meta charset="utf-8">', '<meta charset="utf-8"><link rel="stylesheet" href="x.css">'),
            'second title': p.replace('</head>', '<title>x</title></head>'),
            'second style': p.replace('</head>', '<style>a{}</style></head>'),
            'no robots': p.replace('<meta name="robots" content="noindex, nofollow">\n', ''),
            'no cands id': p.replace('id="cands"', 'id="top2"').replace('id="cands-h"', 'id="cands-h"'),
            'no disclaimer': p.replace('僅供研究，非投資建議', '僅供參考'),
            'svg': p.replace('<main>', '<main><svg></svg>'),
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.assertNotEqual(text, p, name)
                self.assertTrue(share.check_html(text.encode('utf-8'), rules), name)

    def test_encoding_and_size(self):
        rules = self.rules()
        self.assertTrue(share.check_html(b'\xff\xfe<html>', rules))
        self.assertTrue(share.check_html(b'\xef\xbb\xbf' + self.page.encode('utf-8'), rules))
        big = self.inject('<p>' + 'a' * 300000 + '</p>')
        self.assertGreater(len(big), share.MAX_BYTES)
        self.assertTrue(any('太大' in x for x in share.check_html(big, rules)))
        self.assertTrue(share.check_html(self.page.encode('utf-16'), rules))

    def test_username_and_hostname(self):
        with mock.patch.dict(os.environ, {'USERNAME': 'zeta_user', 'USER': '', 'LOGNAME': '', 'COMPUTERNAME': 'BOXY-01',
                                           'HOSTNAME': ''}), \
                mock.patch.object(share.socket, 'gethostname', return_value='HOSTNAME-XYZ'), \
                mock.patch.object(share.getpass, 'getuser', return_value='zeta_user'):
            rules = share.make_rules(capital=None)
            self.assertEqual(share.check_html(self.page.encode('utf-8'), rules), [])
            for secret in ('zeta_user', 'ZETA_USER', 'BOXY-01', 'HOSTNAME-XYZ'):
                with self.subTest(secret):
                    probs = share.check_html(self.inject('<p>hi %s ok</p>' % secret), rules)
                    self.assertTrue(probs)
                    self.assertNotIn(secret.lower(), '\n'.join(probs).lower())
            # 名稱只是別的字的一部分時不算
            self.assertEqual(share.check_html(self.inject('<p>xzeta_users</p>'), rules), [])

    def test_watchlist_masks(self):
        wl = {'2301': '光寶科', '1022': '某股', '2385': '某某', '2330': '台積電', '1101': '台泥'}
        rules = share.make_rules(capital=None, watchlist=wl)
        page = self.page.replace('掃描 7 檔', '掃描 2301 檔')
        self.assertIn('掃描 2301 檔', page)
        self.assertEqual(share.check_html(page.encode('utf-8'), rules), [])          # 「N 檔」的數字不算
        self.assertNotIn('1022', page.replace('2026-10-22', ''))
        self.assertIn('2026-10-22', page)                                              # 日期不算
        self.assertIn('2385.0', page)                                                  # 價格 2385.0 不是代號 2385
        # 自選股裡的代號剛好也是今天的候選：可以出現（Q8）
        self.assertIn('data-code="2330"', page)
        probs = share.check_html(self.inject('<p>看 2301 這檔</p>'), rules)
        self.assertTrue(probs)
        self.assertNotIn('2301', '\n'.join(probs))
        self.assertTrue(share.check_html(self.inject('<p>光寶科</p>'), rules))
        self.assertEqual(share.check_html(self.inject('<p>X</p>'), share.make_rules(watchlist={'2330': '台積電'})), [])

    def test_make_rules_small_capital_not_value_matched(self):
        rules = share.make_rules(capital=5000)
        self.assertEqual(share.check_html(self.inject('<p>5000</p>'), rules), [])
        self.assertTrue(share.check_html(self.inject('<p>capital=5000</p>'), rules))

    # v1.1.5：Unicode 正規化（NFKC）、中文數字、零寬字元
    HOSTILE = [   # (名稱, 寫法, make_rules 的參數；None = 用預設的 capital=1000000)
        ('full-width digits', '１０００００００', dict(capital=10000000)),
        ('full-width wan', '１００萬', None),
        ('space sep', '1 000 000', None),
        ('nbsp sep', '1 000 000', None),
        ('thin space sep', '1 000 000', None),
        ('ideographic space sep', '1　000　000', None),
        ('underscore sep', '1_000_000', None),
        ('comma decimals', '1,000,000.00', None),
        ('dot thousands', '1.000.000', None),
        ('trailing .00', '1000000.00', None),
        ('sci 1.0e6', '1.0e6', None),
        ('sci 1e6', '1e6', None),
        ('sci E+06', '1E+06', None),
        ('cn 一百萬', '一百萬', None),
        ('cn 壹佰萬', '壹佰萬', None),
        ('cn 兩百萬', '兩百萬', dict(capital=2000000)),
        ('cn 二百五十萬', '二百五十萬', dict(capital=2500000)),
        ('cn 一千萬', '一千萬', dict(capital=10000000)),
        ('arabic 1,000萬', '1,000萬', dict(capital=10000000)),
        ('arabic 1百萬', '1百萬', None),
        ('cn 一百二十三萬四千五百', '一百二十三萬四千五百', dict(capital=1234500)),
        ('cn 一億', '一億', dict(capital=100000000)),
        ('cn 零', '一百萬零五百', dict(capital=1000500)),
        ('cn 〇', '一〇〇〇〇〇〇', None),
        ('amount full-width comma', '５０，０００', None),
        ('amount cn', '五萬', None),
        ('amount spaced', '1 234 567', None),
        ('full-width email', 'bobby＠example.com', None),
        ('full-width drive path', 'Ｃ：＼Ｕｓｅｒｓ＼x', None),
        ('full-width config key', 'ｃａｐｉｔａｌ＝5', None),
        ('full-width url', 'ｈｔｔｐｓ：／／evil.example', None),
    ]
    INVISIBLE = [   # 含零寬或格式字元：share_rows 會先拒絕，直接放進頁面也要擋
        ('zero-width split digits', '1000​000', None),
        ('word joiner digits', '1⁠000⁠000', None),
        ('soft hyphen digits', '1­000­000', None),
        ('zero-width split word', '部​位', None),
        ('bom split key', 'capi﻿tal=5', None),
    ]

    def assert_flagged(self, name, secret, probs):
        self.assertTrue(probs, name)
        joined = '\n'.join(probs)
        for part in {secret, share._nf(secret), share._strip_cf(share._nf(secret))}:
            if len(part) >= 3:
                self.assertNotIn(part, joined, name)

    def test_unicode_and_numeral_forms_blocked_in_every_field(self):
        for name, secret, kw in self.HOSTILE:
            rules = self.rules(**(kw or {}))
            self.assertEqual(share.check_html(self.page.encode('utf-8'), rules), [], name)
            for field in ('name', 'setup', 'stop_why'):
                with self.subTest(name, field=field):
                    page = make_page(tweak=lambda res, f=field, s=secret: res[0].update({f: '台泥 ' + s + ' 收紅'}))
                    self.assert_flagged(name, secret, share.check_html(page.encode('utf-8'), rules))
            with self.subTest(name, field='crafted html'):
                self.assert_flagged(name, secret, share.check_html(self.inject('<p>看 %s 這裡</p>' % secret), rules))

    def test_invisible_characters_blocked(self):
        for name, secret, kw in self.INVISIBLE:
            rules = self.rules(**(kw or {}))
            for field in ('name', 'setup', 'stop_why'):
                with self.subTest(name, field=field):
                    with self.assertRaises(ValueError):
                        make_page(tweak=lambda res, f=field, s=secret: res[0].update({f: '台泥' + s}))
            with self.subTest(name, field='crafted html'):
                probs = share.check_html(self.inject('<p>看 %s 這裡</p>' % secret), rules)
                self.assert_flagged(name, secret, probs)
                self.assertTrue(any('控制字元' in p for p in probs), probs)
                self.assertGreaterEqual(len(probs), 2, probs)      # 除了控制字元，內容本身也被認出來

    def test_share_rows_rejects_format_and_control_chars(self):
        for ch in ('​', '‌', '‍', '⁠', '﻿', '­', '‮', '‪', '⁦', '⁩',
                   '‎', '؜', '', '\U000f0000', '͸', ' ', ' ', '\x85', '\x1b', '\x7f'):
            for field in ('name', 'setup', 'stop_why'):
                with self.subTest(hex(ord(ch)), field=field):
                    with self.assertRaises(ValueError):
                        make_page(tweak=lambda res, f=field, c=ch: res[0].update({f: '台' + c + '泥'}))
            with self.subTest(hex(ord(ch)), field='crafted html'):
                probs = share.check_html(self.inject('<p>a%sb</p>' % ch), share.make_rules(capital=None))
                self.assertTrue(any('控制字元' in p for p in probs), probs)
        # 一般的全形字、全形空白、表情以外的常見符號照樣可以用
        page = make_page(tweak=lambda res: res[0].update(name='台泥　Ａ－１', setup='量比 ２．４，收紅 ▲'))
        self.assertEqual(share.check_html(page.encode('utf-8'), self.rules()), [])

    def test_unicode_negatives_not_flagged(self):
        rules = self.rules(capital=1000000, amounts=[50000, 1234567])
        cases = ['2385', '1234.5', '1000', '1 000', '100000', '2026-10-22', '2026/10/22', '共 2301 檔', '一百檔', '十大',
                 '三十萬', '十萬', '一百二十三萬', '五百萬', '一千', '一億', '百萬', '千萬別追高', '萬海', '一張一張', '二〇二六年',
                 '1e3', '2e6', '12.345', '50.000', '收盤 23，500', '量比 1.5，500 張', '0.1000000', '1000000.5',
                 '1000.000', '21000000', '12345678', 'MA20 1000', '第 1 關 000', '１２３４．５',
                 '一百二十三萬四千', 'Ｃ＃', '５０％', 'ａｂｃ＠ｘ']
        for s in cases:
            for field in ('name', 'setup', 'stop_why'):
                with self.subTest(s, field=field):
                    page = make_page(tweak=lambda res, f=field, v=s: res[0].update({f: '台泥 ' + v}))
                    self.assertEqual(share.check_html(page.encode('utf-8'), rules), [], s)
        # CSS 的數字（rgba、逗號）不算千分位，也不是金額
        css = self.page.replace('</style>', 'a{box-shadow:0 1px 2px rgba(255,255,255,.08);font:600 15px/1.4 x;margin:1,000px}</style>', 1)
        self.assertNotEqual(css, self.page)
        self.assertEqual(share.check_html(css.encode('utf-8'), rules), [])
        # 中文數字和設定的金額無關：不擋
        self.assertEqual(share.check_html(self.inject('<p>一百萬</p>'), self.rules(capital=2000000)), [])
        self.assertEqual(share.check_html(self.inject('<p>1 000 000</p>'), self.rules(capital=2000000)), [])

    def test_cn_numeral_parser(self):
        last = lambda s: list(share._cn_values(s))[-1]
        for s, v in (('一百二十三萬四千五百', 1234500), ('一億', 10 ** 8), ('壹佰萬', 10 ** 6), ('二百五十萬', 2500000),
                     ('一〇〇〇〇〇〇', 10 ** 6), ('十萬', 100000), ('一千零五', 1005), ('3萬4千', 34000), ('兩百萬', 2 * 10 ** 6),
                     ('一億二千萬', 120000000), ('1百萬', 10 ** 6), ('十大', 10), ('一百檔', 100), ('萬', 0), ('百萬', 0),
                     ('贰佰万', 2 * 10 ** 6), ('叁亿', 3 * 10 ** 8)):
            self.assertEqual(last(s.replace('檔', '').replace('大', '')), v, s)
        self.assertEqual(list(share._cn_values('萬' * 10000))[-1], 0)
        self.assertLess(len(list(share._cn_values('九億' * 50))), 50)       # 太大就停

    def test_leak_check_speed_on_300kb_adversarial_input(self):
        import time
        rules = self.rules(amounts=list(range(10000, 10060)) + [1234567])
        n = share.MAX_BYTES - len(self.page.encode('utf-8'))
        cases = {'a@': 'a@' * (n // 2), 'digits': '1' * n, 'digit space': '1 ' * (n // 2), 'digit comma': '1,' * (n // 2),
                 'digit dot': '1.' * (n // 2), 'long spaces': '1' + ' ' * (n - 2) + '1', 'cn': '一' * (n // 3),
                 'wan': '萬' * (n // 3), 'cn mix': '一百萬1' * (n // 12), 'a.': 'a.' * (n // 2), 'zero width': '1​' * (n // 4),
                 'e': '1e' * (n // 2), 'entities': '&#49;' * (n // 5), 'full-width': '１' * (n // 3), '000 ': '000 ' * (n // 4),
                 'slashes': '/' * n, 'users': '/users/' * (n // 7), 'digits then 檔': '1' * (n - 3) + '檔'}
        for name, text in cases.items():
            with self.subTest(name):
                data = self.inject('<p>%s</p>' % text)
                t = time.perf_counter()
                probs = share.check_html(data, rules)
                self.assertLess(time.perf_counter() - t, 3.0, name)
                self.assertIsInstance(probs, list)

    def test_unit_helpers(self):
        self.assertEqual(share.blob_sha(b'hello\n'), 'ce013625030ba8dba906f756967f9e9ca394464a')
        self.assertEqual(share.pages_url('YourName', 'res'), 'https://yourname.github.io/res/')
        self.assertEqual(share.pages_url('yourname', 'YourName.github.io'), 'https://yourname.github.io/')
        r = share._clean('failed ghp_' + 'a' * 36 + ' and github_pat_' + 'B' * 30 + ' https://user:pw@github.com/x '
                         'Authorization: Bearer abcdef ' + 'z' * 500)
        self.assertNotIn('ghp_', r)
        self.assertNotIn('github_pat_', r)
        self.assertNotIn('user:pw', r)
        self.assertNotIn('abcdef', r)
        self.assertLessEqual(len(r), 301)

    def test_latest_files(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(share.latest_files(d), (None, None, None))
            for n in ('分享版_20261021.html', '分享版_20261022.html', '盤後選股_20261022.html', '盤後選股_20261023.html',
                      '分享版_x.html', '分享版_20261024.html.tmp'):
                tu.write(os.path.join(d, n), 'x')
            p, tag, rt = share.latest_files(d)
            self.assertEqual((os.path.basename(p), tag, rt), ('分享版_20261022.html', '20261022', '20261023'))
            self.assertEqual(share.latest_files(os.path.join(d, 'nope')), (None, None, None))

    def test_state_roundtrip_has_no_unknown_fields(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'a', 'state.json')
            self.assertEqual(share.load_state(p), {})
            share.save_state(p, dict(repo='a/b', confirmed=True, token='ghp_secret', last_blob='x'))
            st = share.load_state(p)
            self.assertEqual(st, dict(repo='a/b', confirmed=True, last_blob='x'))
            self.assertNotIn(b'ghp_', tu.readb(p))
            tu.write(p, 'not json')
            self.assertEqual(share.load_state(p), {})


# ---------- scan --share ----------
def fake_analyze_factory(tweak=None):
    recs = {r['code']: r for r in tu.fake_results(capital=None)}
    if tweak:
        tweak(recs)

    def fake(code, name, bars):
        r = recs.get(code)
        return copy.deepcopy(r) if r else dict(code=code, name=name, error='沒有這檔')
    return fake


class TestScanShare(tu.TempCase):
    CODES = ('1101', '2330', '00646', '6874', '2454', '1216')

    def setUp(self):
        super().setUp()
        self.data = os.path.join(self.tmp, 'data')
        tu.make_data(self.data, self.CODES)
        self.work = os.path.join(self.tmp, 'work')
        os.makedirs(self.work)
        self.tag = '20261022'

    def run_scan(self, tweak=None, **k):
        with mock.patch.object(screener, 'analyze', fake_analyze_factory(tweak)), Chdir(self.work):
            return tu.quiet(screener.cmd_scan, ns(data=self.data, **k))

    def cfg(self, text):
        return tu.write(os.path.join(self.tmp, '設定.txt'), text)

    def test_share_via_config_writes_passing_file(self):
        out = os.path.join(self.tmp, '結果')
        cfg = self.cfg('capital=1000000\nrisk=3%%\nshare=1\noutput_dir=%s\n' % out)
        _, err = self.run_scan(config=cfg)
        p = os.path.join(out, '分享版_%s.html' % self.tag)
        self.assertTrue(os.path.exists(p), err)
        raw = tu.readb(p)
        self.assertFalse(raw.startswith(b'\xef\xbb\xbf'))
        self.assertEqual(share.check_html(raw, share.make_rules(capital=1000000, paths=[self.tmp, ROOT],
                                                                 sectors=screener.SECTOR_LABELS)), [])
        self.assertNotIn(b'1000000', raw)
        self.assertIn('分享版：' + os.path.abspath(p), err)
        self.assertEqual(raw.decode('utf-8'), make_page(tweak=lambda res: (res.pop(), screener.attach_sectors(res, {}))))     # 和直接產生的一樣（少了 9999 那筆）
        self.assertFalse([n for n in os.listdir(out) if n.endswith('.tmp')])

    def test_share_publish_in_config_also_generates(self):
        out = os.path.join(self.tmp, '結果')
        cfg = self.cfg('share_publish=1\nshare_repo=yourname/res\noutput_dir=%s\n' % out)
        self.run_scan(config=cfg)
        self.assertTrue(os.path.exists(os.path.join(out, '分享版_%s.html' % self.tag)))

    def test_share_flag_without_config(self):
        out = os.path.join(self.tmp, 'o')
        self.run_scan(out_dir=out, share=True)
        self.assertTrue(os.path.exists(os.path.join(out, '分享版_%s.html' % self.tag)))

    def test_share_off_by_default(self):
        out = os.path.join(self.tmp, 'o')
        _, err = self.run_scan(out_dir=out)
        self.assertEqual(len(os.listdir(out)), 5)
        self.assertNotIn('分享版', err)

    def test_list_mode_writes_none_and_warns(self):
        lst = tu.write(os.path.join(self.tmp, 'l.csv'), '代號,名稱\n1101,台泥\n2330,台積電\n')
        out = os.path.join(self.tmp, 'o')
        _, err = self.run_scan(out_dir=out, share=True, list=lst)
        self.assertEqual(len(os.listdir(out)), 5)
        self.assertIn('只支援全市場', err)

    def test_no_out_dir_warns(self):
        out = os.path.join(self.tmp, 'x.md')
        _, err = self.run_scan(out=out, share=True)
        self.assertIn('沒有結果資料夾', err)
        self.assertEqual(os.listdir(self.tmp).count('分享版_%s.html' % self.tag), 0)

    def test_blocked_writes_nothing_and_removes_old_same_day_file(self):
        out = os.path.join(self.tmp, 'o')
        stale = tu.write(os.path.join(out, '分享版_%s.html' % self.tag), 'old content')
        other_day = tu.write(os.path.join(out, '分享版_20261021.html'), 'older day')

        def tweak(recs):
            recs['1101']['setup'] = '資金 1,000,000 元'
        _, err = self.run_scan(tweak=tweak, out_dir=out, share=True)
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(other_day))
        self.assertEqual(len([n for n in os.listdir(out) if n.startswith('盤後選股_')]), 5)
        self.assertIn('沒有產生', err)
        self.assertNotIn('1,000,000', err)
        self.assertLessEqual(len([l for l in err.splitlines() if l.startswith('  - ') or '分享版' in l]), 8)

    def test_blocked_by_bad_data_still_exits_normally(self):
        out = os.path.join(self.tmp, 'o')

        def tweak(recs):
            recs['1101']['close'] = float('nan')
        _, err = self.run_scan(tweak=tweak, out_dir=out, share=True)
        self.assertIn('沒有產生', err)
        self.assertFalse(os.path.exists(os.path.join(out, '分享版_%s.html' % self.tag)))
        self.assertTrue(os.path.exists(os.path.join(out, '盤後選股_%s.md' % self.tag)))

    def test_other_outputs_byte_identical_with_and_without_share(self):
        a, b = os.path.join(self.tmp, 'a'), os.path.join(self.tmp, 'b')
        self.run_scan(out_dir=a, capital=1000000.0, risk=0.03)
        self.run_scan(out_dir=b, capital=1000000.0, risk=0.03, share=True)
        self.assertEqual(len(os.listdir(a)), 5)
        self.assertEqual(len(os.listdir(b)), 6)
        mask = lambda s: re.sub(r'產生時間 \d{4}-\d{2}-\d{2} \d{2}:\d{2}'.encode(), '產生時間 X'.encode(), s)
        for ext in ('md', 'json', 'txt', 'csv', 'html'):
            n = '盤後選股_%s.%s' % (self.tag, ext)
            x, y = tu.readb(os.path.join(a, n)), tu.readb(os.path.join(b, n))
            self.assertEqual(mask(x) if ext == 'html' else x, mask(y) if ext == 'html' else y, ext)

    def test_subprocess_scan_share_with_config_yields_six_files(self):
        out = os.path.join(self.tmp, '結果')
        cfg = self.cfg('capital=1000000\nshare=1\noutput_dir=%s\n' % out)
        env = tu.clean_env(PYTHONUTF8='1')
        r = subprocess.run([sys.executable, '-B', os.path.join(ROOT, 'screener.py'), 'scan', '--data', self.data, '--config', cfg,
                            '--share'], cwd=self.work, env=env, capture_output=True, timeout=120)
        err = r.stderr.decode('utf-8', 'replace')
        self.assertEqual(r.returncode, 0, err)
        names = sorted(os.listdir(out))
        self.assertEqual(len(names), 6, names)
        self.assertEqual(len([n for n in names if n.startswith('分享版_')]), 1)
        self.assertIn('分享版：', err)

    def test_resolve_scan_options_share(self):
        o = screener.resolve_scan_options(argparse.Namespace(config=None, capital=None, risk=None, list=None, out_dir=None))
        self.assertFalse(o['share'])
        cfg = self.cfg('share=1\n')
        o = screener.resolve_scan_options(argparse.Namespace(config=cfg, capital=None, risk=None, list=None, out_dir=None))
        self.assertTrue(o['share'])
        o = screener.resolve_scan_options(argparse.Namespace(config=None, capital=None, risk=None, list=None, out_dir=None,
                                                             share=True))
        self.assertTrue(o['share'])


# ---------- 公開／下架（gh 全部是假的） ----------
class FakeGh:
    """假的 subprocess.run：記錄每次呼叫，依參數回應像 gh 的結果"""

    def __init__(self, login=LOGIN, uid=4242):
        self.calls, self.login, self.uid = [], login, uid
        self.repo, self.files, self.pages = None, {}, False
        self.script, self.post_pages, self.repo_404_after_create = [], [], 0

    @staticmethod
    def ok(obj=None):
        return 0, (json.dumps(obj).encode('utf-8') if obj is not None else b''), b''

    @staticmethod
    def err(code, msg='error'):
        return 1, b'', ('gh: %s (HTTP %d)' % (msg, code)).encode('utf-8')

    def argvs(self):
        return [c[0][1:] for c in self.calls]

    def puts(self):
        return [a for a in self.argvs() if '-X' in a and a[a.index('-X') + 1] == 'PUT']

    def named(self, *words):
        return [a for a in self.argvs() if all(w in a for w in words)]

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        a = list(argv[1:])
        for pred, resp in self.script:
            if pred(a):
                rc, out, err = resp(a) if callable(resp) else resp
                return subprocess.CompletedProcess(argv, rc, out, err)
        rc, out, err = self.dispatch(a, kw)
        return subprocess.CompletedProcess(argv, rc, out, err)

    def dispatch(self, a, kw):
        if a[:2] == ['repo', 'create']:
            self.repo = dict(private=False, archived=False, default_branch='main')
            self.repo_404_after_create = 1
            return self.ok()
        if a[:2] == ['repo', 'delete']:
            self.repo = None
            return self.ok()
        assert a[0] == 'api', a
        method = a[a.index('-X') + 1] if '-X' in a else 'GET'
        path = next(x for x in a[1:] if x.startswith('repos/') or x == 'user')
        if path == 'user':
            return self.ok(dict(login=self.login, id=self.uid))
        m = re.fullmatch(r'repos/([^/]+)/([^/]+)(/.*)?', path)
        sub = m.group(3) or ''
        if sub == '':
            if self.repo is None:
                return self.err(404, 'Not Found')
            if self.repo_404_after_create:
                self.repo_404_after_create = 0
                return self.err(404, 'Not Found')
            return self.ok(self.repo)
        if sub == '/contents/':
            if self.repo is None or not self.files:
                return self.err(404, 'This repository is empty.')
            return self.ok([dict(name=n) for n in self.files])
        if sub.startswith('/contents/'):
            name = sub[len('/contents/'):]
            if method == 'PUT':
                body = json.loads(kw['input'].decode('utf-8'))
                if name in self.files and body.get('sha') != share.blob_sha(self.files[name]):
                    return self.err(409, 'sha mismatch')
                self.files[name] = base64.b64decode(body['content'])
                self.last_put = body
                return self.ok(dict(content=dict(sha=share.blob_sha(self.files[name]))))
            if name not in self.files:
                return self.err(404, 'Not Found')
            return self.ok(dict(name=name, sha=share.blob_sha(self.files[name])))
        if sub == '/pages':
            if method == 'DELETE':
                self.pages = False
                return 0, b'', b''
            if method == 'POST':
                st = self.post_pages.pop(0) if self.post_pages else 201
                if st >= 400:
                    return self.err(st, 'pages')
                self.pages = True
                return self.ok(dict(html_url='x'))
            return self.ok(dict(status='built')) if self.pages else self.err(404, 'Not Found')
        raise AssertionError(a)


class GhCase(tu.TempCase):
    tty = False
    answers = ()

    def setUp(self):
        super().setUp()
        self.out = os.path.join(self.tmp, '結果')
        self.state = os.path.join(self.tmp, 'cache', 'share', 'state.json')
        self.write_day('20261022')
        self.rules = share.make_rules(capital=1000000, amounts=[50000], paths=[self.tmp], watchlist={'2317': '鴻海'}, repo=REPO)
        self.gh = FakeGh()
        self.said, self.asked, self.sleeps = [], [], []
        self.answers = list(self.answers)
        for name, val in (('_run', self.gh), ('_which', lambda n: FAKE_GH if n in ('gh.exe', 'gh') else None),
                          ('_sleep', self.sleeps.append), ('_isatty', lambda: self.tty), ('_ask', self.fake_ask)):
            p = mock.patch.object(share, name, val)
            p.start()
            self.addCleanup(p.stop)

    def fake_ask(self, prompt):
        self.asked.append(prompt)
        return self.answers.pop(0) if self.answers else ''

    def write_day(self, tag, page=None, report=True):
        date = '%s-%s-%s' % (tag[:4], tag[4:6], tag[6:])
        page = page if page is not None else make_page(date)
        tu.write(os.path.join(self.out, '分享版_%s.html' % tag), page)
        if report:
            tu.write(os.path.join(self.out, '盤後選股_%s.html' % tag), '<html>full report</html>')
        return page.encode('utf-8') if isinstance(page, str) else page

    def publish(self, repo=REPO, mode='full', **k):
        return share.publish(repo, mode, self.out, self.state, self.rules, say=self.said.append, **k)

    def first_publish(self):
        self.tty, self.answers = True, ['Y']
        self.assertEqual(self.publish(ask=True), 0)
        self.tty, self.answers = False, []

    def said_text(self):
        return '\n'.join(self.said)

    def assert_error(self, code, fn, *a, **k):
        with self.assertRaises(share.ShareError) as cm:
            fn(*a, **k)
        self.assertEqual(cm.exception.code, code, cm.exception.cause)
        return cm.exception


class TestPublishConfirm(GhCase):
    def test_unconfirmed_without_ask_returns_10_and_zero_calls(self):
        self.assertEqual(self.publish(), share.E_CONFIRM)
        self.assertEqual(self.gh.calls, [])
        self.assertFalse(os.path.exists(self.state))
        t = self.said_text()
        for s in ('【第一次公開前的確認】還沒有公開任何東西，也還沒有連到 GitHub', REPO, 'https://yourname.github.io/tw-stock-screener-results/',
                  'index.html', 'README.md', '安全檢查：通過', '分享版_20261022.html', '--confirm-first-publish ' + REPO,
                  '總資金、部位試算、觀察名單、自選股', '無法收回', '候選標的 4 檔', '2026-10-22'):
            self.assertIn(s, t, s)

    def test_ask_but_not_tty_returns_10(self):
        self.answers = ['Y']
        self.assertEqual(self.publish(ask=True), 10)
        self.assertEqual((self.gh.calls, self.asked), ([], []))

    def test_tty_and_answer_no(self):
        for ans in ('N', '', 'no', 'y e s'):
            with self.subTest(ans):
                self.tty, self.answers = True, [ans]
                self.assertEqual(self.publish(ask=True), 10)
                self.assertEqual(self.gh.calls, [])
                self.assertEqual(len(self.asked), 1)
                self.asked.clear()
        self.assertFalse(os.path.exists(self.state))

    def test_tty_without_ask_flag_does_not_ask(self):
        self.tty, self.answers = True, ['Y']
        self.assertEqual(self.publish(), 10)
        self.assertEqual((self.gh.calls, self.asked), ([], []))

    def test_eof_while_asking(self):
        self.tty = True
        with mock.patch.object(share, '_ask', side_effect=EOFError):
            self.assertEqual(self.publish(ask=True), 10)
        self.assertEqual(self.gh.calls, [])

    def test_wrong_token_is_15_with_zero_calls(self):
        for t in ('other/repo', '', 'yourname/tw-stock-screener-results-x'):
            e = self.assert_error(15, self.publish, token=t)
            self.assertEqual(self.gh.calls, [])
        self.assertTrue(e.todo)

    def test_missing_gh_still_shows_preview(self):
        with mock.patch.object(share, '_which', lambda n: None):
            self.assertEqual(self.publish(), 10)
        self.assertIn('安全檢查：通過', self.said_text())

    def test_old_other_repo_noted_in_preview(self):
        share.save_state(self.state, dict(repo='someone/old-one', confirmed=True, last_blob='x'))
        self.assertEqual(self.publish(), 10)
        self.assertIn('someone/old-one', self.said_text())
        self.assertEqual(self.gh.calls, [])


class TestPublishFlow(GhCase):
    def test_first_publish_creates_repo_and_uploads(self):
        page = self.write_day('20261022')
        self.tty, self.answers = True, ['Y']
        self.assertEqual(self.publish(ask=True), 0)
        g = self.gh
        self.assertEqual(len(g.named('repo', 'create')), 1)
        create = g.named('repo', 'create')[0]
        self.assertIn('--public', create)
        self.assertEqual(create[2], REPO)
        self.assertIn('--disable-issues', create)
        puts = g.puts()
        self.assertEqual(sorted(p[-3].split('/')[-1] for p in puts), ['README.md', 'index.html'])
        self.assertEqual(len(g.named('-X', 'POST', 'repos/%s/pages' % REPO)), 1)
        self.assertEqual(g.files['index.html'], page)
        self.assertIn(b'index.html', g.files['README.md'])
        self.assertIn('https://yourname.github.io/tw-stock-screener-results/'.encode(), g.files['README.md'])
        self.assertNotIn(b'github.com/', g.files['README.md'])
        st = share.load_state(self.state)
        self.assertEqual((st['repo'], st['confirmed'], st['login'], st['last_date'], st['last_blob']),
                         (REPO, True, LOGIN, '2026-10-22', share.blob_sha(page)))
        self.assertEqual(st['pages_url'], 'https://yourname.github.io/tw-stock-screener-results/')
        self.assertEqual(set(st), set(share.STATE_FIELDS))
        self.assertIn('https://yourname.github.io/tw-stock-screener-results/', self.said_text())
        self.assertIn('1～10 分鐘', self.said_text())

    def test_call_hygiene(self):
        self.first_publish()
        self.assertTrue(self.gh.calls)
        for argv, kw in self.gh.calls:
            self.assertIsInstance(argv, list)
            self.assertEqual(argv[0], FAKE_GH)
            self.assertIsNot(kw.get('shell'), True)
            self.assertEqual(kw.get('timeout'), 60)
            self.assertEqual(kw.get('cwd'), tempfile.gettempdir())
            for x in argv:
                self.assertLess(len(x), 1000, x[:50])
            env = kw['env']
            for k, v in (('GH_HOST', 'github.com'), ('GH_PROMPT_DISABLED', '1'), ('GH_NO_UPDATE_NOTIFIER', '1'),
                         ('NO_COLOR', '1'), ('GH_SPINNER_DISABLED', '1')):
                self.assertEqual(env.get(k), v)
            self.assertNotIn('auth', argv[1:2])
            self.assertFalse(any('token' in x for x in argv[1:]))
        for a in self.gh.puts():
            self.assertIn(a[a.index('--input') - 1].split('/')[-1], ('index.html', 'README.md'))
            self.assertEqual(a[a.index('--input') + 1], '-')

    def test_commit_identity_is_noreply(self):
        self.first_publish()
        body = self.gh.last_put
        want = dict(name=LOGIN, email='4242+%s@users.noreply.github.com' % LOGIN)
        self.assertEqual((body['committer'], body['author']), (want, want))
        self.assertEqual(body['branch'], 'main')
        for c in self.gh.calls:
            if c[1].get('input'):
                b = json.loads(c[1]['input'].decode('utf-8'))
                self.assertNotIn('isleohsieh', json.dumps(b))
                self.assertEqual(b['committer']['email'].split('@')[1], 'users.noreply.github.com')

    def test_token_confirm_works_case_insensitive(self):
        self.assertEqual(self.publish(token=REPO.upper()), 0)
        self.assertEqual(len(self.gh.named('repo', 'create')), 1)

    def test_same_blob_second_run_makes_no_calls(self):
        self.first_publish()
        n = len(self.gh.calls)
        self.said.clear()
        self.assertEqual(self.publish(), 0)
        self.assertEqual(len(self.gh.calls), n)
        self.assertIn('分享頁已經是最新的（資料日期 2026-10-22），不用更新。', self.said_text())

    def test_new_day_only_updates_index(self):
        self.first_publish()
        self.gh.calls.clear()
        page2 = self.write_day('20261023')
        self.assertEqual(self.publish(), 0)
        puts = self.gh.puts()
        self.assertEqual(len(puts), 1)
        self.assertTrue(puts[0][-3].endswith('/index.html') or 'repos/%s/contents/index.html' % REPO in puts[0])
        self.assertEqual(self.gh.last_put['sha'], share.blob_sha(make_page('2026-10-22').encode()))
        self.assertEqual(self.gh.files['index.html'], page2)
        self.assertEqual(self.gh.named('repo', 'create'), [])
        self.assertEqual(self.gh.named('-X', 'POST'), [])
        self.assertEqual(share.load_state(self.state)['last_date'], '2026-10-23')

    def test_older_data_makes_no_calls(self):
        self.first_publish()
        share.save_state(self.state, dict(share.load_state(self.state), last_date='2026-10-25', last_blob='zzz'))
        self.gh.calls.clear()
        self.assertEqual(self.publish(), 0)
        self.assertEqual(self.gh.calls, [])

    def test_existing_remote_same_content_skips_put(self):
        page = self.write_day('20261022')
        self.gh.repo = dict(private=False, archived=False, default_branch='main')
        self.gh.files = {'index.html': page, 'README.md': b'hello'}
        self.gh.pages = True
        self.assertEqual(self.publish(token=REPO), 0)
        self.assertEqual(self.gh.puts(), [])
        self.assertEqual(self.gh.named('repo', 'create'), [])
        self.assertEqual(self.gh.named('-X', 'POST'), [])
        self.assertTrue(share.load_state(self.state)['confirmed'])

    def test_existing_repo_readme_only(self):
        self.gh.repo = dict(private=False, archived=False, default_branch='trunk')
        self.gh.files = {'README.md': b'hi'}
        self.assertEqual(self.publish(token=REPO), 0)
        self.assertEqual(len(self.gh.puts()), 1)
        self.assertEqual(self.gh.last_put['branch'], 'trunk')
        self.assertIn('source[branch]=trunk', self.gh.named('-X', 'POST')[0])

    def test_repo_get_404_after_create_retries_once(self):
        self.assertEqual(self.publish(token=REPO), 0)
        self.assertEqual(self.sleeps, [3])
        self.assertEqual(len(self.gh.named('repo', 'create')), 1)

    def test_pages_409_is_fine(self):
        self.gh.post_pages = [409]
        self.assertEqual(self.publish(token=REPO), 0)
        self.assertEqual(len(self.gh.named('-X', 'POST')), 1)

    def test_pages_422_retries_after_sleep(self):
        self.gh.post_pages = [422, 201]
        self.assertEqual(self.publish(token=REPO), 0)
        self.assertEqual(len(self.gh.named('-X', 'POST')), 2)
        self.assertIn(3, self.sleeps)

    def test_pages_404_then_ok_retry(self):
        self.gh.post_pages = [404, 201]
        self.assertEqual(self.publish(token=REPO), 0)
        self.assertEqual(len(self.gh.named('-X', 'POST')), 2)

    def test_pages_422_twice_is_14(self):
        self.gh.post_pages = [422, 422]
        self.assert_error(14, self.publish, token=REPO)
        self.assertEqual(len(self.gh.named('-X', 'POST')), 2)

    def test_pages_gone_when_confirmed_is_15_and_state_reset(self):
        self.first_publish()
        self.gh.pages = False
        self.gh.calls.clear()
        self.write_day('20261023')
        self.assert_error(15, self.publish)
        self.assertEqual(self.gh.named('-X', 'POST'), [])
        self.assertEqual(self.gh.named('repo', 'create'), [])
        self.assertFalse(share.load_state(self.state)['confirmed'])
        self.assertEqual(self.publish(), 10)               # 再來要重新確認

    def test_repo_gone_when_confirmed_is_15_and_not_recreated(self):
        self.first_publish()
        self.gh.repo = None
        self.gh.files = {}
        self.gh.calls.clear()
        self.write_day('20261023')
        self.assert_error(15, self.publish)
        self.assertEqual(self.gh.named('repo', 'create'), [])
        self.assertEqual(self.gh.puts(), [])
        self.assertFalse(share.load_state(self.state)['confirmed'])


class TestPublishRefusals(GhCase):
    def test_leak_when_confirmed_is_13_with_zero_calls(self):
        self.first_publish()
        self.gh.calls.clear()
        p = os.path.join(self.out, '分享版_20261022.html')
        tu.write(p, tu.readb(p).decode('utf-8').replace('<main>', '<main><p>1,000,000</p>', 1))
        e = self.assert_error(13, self.publish)
        self.assertEqual(self.gh.calls, [])
        self.assertNotIn('1,000,000', e.cause)

    def test_leak_before_first_publish_is_13_not_10(self):
        p = os.path.join(self.out, '分享版_20261022.html')
        tu.write(p, tu.readb(p).decode('utf-8').replace('<main>', '<main><p>鴻海</p>', 1))
        self.assert_error(13, self.publish)
        self.assertEqual(self.gh.calls, [])

    def test_report_newer_than_share_is_13(self):
        tu.write(os.path.join(self.out, '盤後選股_20261023.html'), 'x')
        e = self.assert_error(13, self.publish, token=REPO)
        self.assertEqual(self.gh.calls, [])
        self.assertTrue(e.todo)

    def test_no_share_file_is_13(self):
        os.remove(os.path.join(self.out, '分享版_20261022.html'))
        self.assert_error(13, self.publish, token=REPO)
        self.assertEqual(self.gh.calls, [])

    def test_gh_missing_is_11(self):
        with mock.patch.object(share, '_which', lambda n: None):
            self.assert_error(11, self.publish, token=REPO)
        self.assertEqual(self.gh.calls, [])

    def test_cmd_shim_is_rejected(self):
        for shim in ('C:\\npm\\gh.cmd', 'C:\\x\\gh.bat'):
            with mock.patch.object(share, '_which', lambda n, s=shim: s if n == 'gh' else None):
                self.assert_error(11, self.publish, token=REPO)
            self.assertEqual(self.gh.calls, [])
        with mock.patch.object(share, '_which', lambda n: 'C:\\x\\gh.cmd'):
            self.assert_error(11, self.publish, token=REPO)

    def test_auth_error_is_12(self):
        self.gh.script.append((lambda a: a[:2] == ['api', 'user'],
                               (4, b'', b'To get started with GitHub CLI, please run:  gh auth login\n')))
        self.assert_error(12, self.publish, token=REPO)
        self.assertEqual(self.gh.puts(), [])
        self.assertEqual(self.gh.named('repo', 'create'), [])

    def test_http_401_is_12(self):
        self.gh.script.append((lambda a: a[:2] == ['api', 'user'], FakeGh.err(401, 'Bad credentials')))
        self.assert_error(12, self.publish, token=REPO)

    def test_owner_must_match_login(self):
        self.gh.login = 'someoneelse'
        e = self.assert_error(15, self.publish, token=REPO)
        self.assertIn('someoneelse', e.cause)
        self.assertEqual(self.gh.puts(), [])
        self.assertEqual(self.gh.named('repo', 'create'), [])
        self.assertEqual(len(self.gh.calls), 1)

    def test_rate_limit_is_14_and_redacted(self):
        secret = 'ghp_' + 'a1' * 20
        self.gh.script.append((lambda a: a[:2] == ['api', 'user'],
                               (1, b'', ('gh: API rate limit exceeded for user (HTTP 403)\nAuthorization: token %s' % secret).encode())))
        e = self.assert_error(14, self.publish, token=REPO)
        self.assertNotIn(secret, e.cause + e.todo)
        self.assertNotIn('ghp_', e.cause)
        self.assertIn('限速', e.cause)

    def test_429_and_403_and_network_and_500(self):
        for resp, text in ((FakeGh.err(429, 'slow down'), '限速'), (FakeGh.err(403, 'Forbidden'), '403'),
                           ((1, b'', b'error connecting to api.github.com'), '連不上'), (FakeGh.err(500, 'oops'), '500')):
            with self.subTest(text):
                self.gh.script[:] = [(lambda a: a[:2] == ['api', 'user'], resp)]
                e = self.assert_error(14, self.publish, token=REPO)
                self.assertIn(text, e.cause)

    def test_long_gh_output_is_truncated(self):
        self.gh.script.append((lambda a: a[:2] == ['api', 'user'], (1, b'', b'x' * 5000 + b' (HTTP 502)')))
        e = self.assert_error(14, self.publish, token=REPO)
        self.assertLess(len(e.cause), 700)

    def test_timeout_is_14(self):
        def boom(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 60)
        with mock.patch.object(share, '_run', boom):
            self.assert_error(14, self.publish, token=REPO)

    def test_repo_with_extra_files_is_15_without_put(self):
        self.gh.repo = dict(private=False, archived=False, default_branch='main')
        self.gh.files = {'index.html': b'x', 'secret.txt': b'y'}
        e = self.assert_error(15, self.publish, token=REPO)
        self.assertIn('secret.txt', e.cause)
        self.assertEqual(self.gh.puts(), [])

    def test_private_or_archived_repo_is_15(self):
        for over in (dict(private=True), dict(archived=True)):
            with self.subTest(over):
                self.gh.repo = dict(dict(private=False, archived=False, default_branch='main'), **over)
                self.assert_error(15, self.publish, token=REPO)
                self.assertEqual(self.gh.puts(), [])

    def test_watchlist_mode_is_15(self):
        e = self.assert_error(15, self.publish, mode='watchlist')
        self.assertIn('只支援全市場（mode=full）', e.cause)
        self.assertEqual(self.gh.calls, [])

    def test_missing_share_repo_is_15(self):
        self.assert_error(15, self.publish, repo=None)
        self.assert_error(15, self.publish, repo='')


class TestTakedown(GhCase):
    def test_non_tty_only_prints(self):
        self.assertEqual(share.takedown(REPO, self.state, self.said.append), 0)
        self.assertEqual(self.gh.calls, [])
        t = self.said_text()
        for s in ('gh api -X DELETE repos/%s/pages' % REPO, 'gh repo delete %s --yes' % REPO, 'delete_repo',
                  '--takedown', 'share_publish'):
            self.assertIn(s, t, s)

    def test_enter_does_nothing(self):
        self.tty, self.answers = True, ['']
        self.assertEqual(share.takedown(REPO, self.state, self.said.append), 0)
        self.assertEqual(self.gh.calls, [])

    def test_wrong_name_does_nothing(self):
        self.tty, self.answers = True, ['yes']
        share.takedown(REPO, self.state, self.said.append)
        self.assertEqual(self.gh.calls, [])

    def test_typed_name_deletes_pages_only(self):
        self.first_publish()
        self.gh.calls.clear()
        self.tty, self.answers = True, [REPO, '']
        self.assertEqual(share.takedown(REPO, self.state, self.said.append), 0)
        self.assertEqual(len(self.gh.calls), 1)
        self.assertEqual(self.gh.argvs()[0], ['api', '-X', 'DELETE', 'repos/%s/pages' % REPO])
        self.assertFalse(self.gh.pages)
        self.assertFalse(share.load_state(self.state)['confirmed'])
        self.assertIsNotNone(self.gh.repo)
        self.assertEqual(len(self.asked), 3)          # 第一次公開的確認 1 次＋下架的 2 次提問

    def test_typed_name_twice_also_deletes_repo(self):
        self.first_publish()
        self.gh.calls.clear()
        self.tty, self.answers = True, [REPO.upper(), REPO]
        share.takedown(REPO, self.state, self.said.append)
        self.assertEqual(self.gh.argvs()[-1], ['repo', 'delete', REPO, '--yes'])
        self.assertIsNone(self.gh.repo)

    def test_cmd_share_takedown_non_tty(self):
        cfg = tu.write(os.path.join(self.tmp, '設定.txt'), 'share_repo=%s\n' % REPO)
        with mock.patch.object(screener, 'SHARE_STATE', self.state):
            rc, err = tu.quiet(screener.cmd_share, argparse.Namespace(config=cfg, publish=False, ask=False,
                                                                       confirm_first_publish=None, takedown=''))
        self.assertEqual(rc, 0)
        self.assertIn('gh api -X DELETE repos/%s/pages' % REPO, err)
        self.assertEqual(self.gh.calls, [])

    def test_takedown_without_repo_is_15(self):
        self.assert_error(15, share.takedown, None, self.state, self.said.append)


class TestCmdShare(GhCase):
    def setUp(self):
        super().setUp()
        self.cfgtext = 'capital=1000000\nshare=1\nshare_publish=1\nshare_repo=%s\noutput_dir=%s\n' % (REPO, self.out)
        self.work = os.path.join(self.tmp, 'work')
        os.makedirs(self.work)
        p = mock.patch.object(screener, 'SHARE_STATE', self.state)
        p.start()
        self.addCleanup(p.stop)

    def cfg(self, text=None):
        return tu.write(os.path.join(self.tmp, '設定.txt'), self.cfgtext if text is None else text)

    def call(self, cfg, **k):
        a = dict(config=cfg, publish=True, ask=False, confirm_first_publish=None, takedown=None)
        a.update(k)
        with Chdir(self.work):
            return tu.quiet(screener.cmd_share, argparse.Namespace(**a))

    def test_publish_off_is_silent_zero(self):
        rc, err = self.call(self.cfg(self.cfgtext.replace('share_publish=1', 'share_publish=0')))
        self.assertEqual((rc, err, self.gh.calls), (0, '', []))

    def test_publish_off_with_confirm_is_15(self):
        cfg = self.cfg(self.cfgtext.replace('share_publish=1', 'share_publish=0'))
        with self.assertRaises(screener.UserError) as cm:
            self.call(cfg, publish=False, confirm_first_publish=REPO)
        self.assertEqual(cm.exception.code, 15)
        self.assertEqual(self.gh.calls, [])

    def test_unconfirmed_returns_10(self):
        rc, err = self.call(self.cfg())
        self.assertEqual(rc, 10)
        self.assertIn('第一次公開前的確認', err)
        self.assertEqual(self.gh.calls, [])
        self.assertFalse(os.path.exists(self.state))

    def test_confirm_flag_publishes(self):
        rc, err = self.call(self.cfg(), publish=False, confirm_first_publish=REPO)
        self.assertEqual(rc, 0, err)
        self.assertTrue(share.load_state(self.state)['confirmed'])

    def test_share_error_becomes_user_error_with_code(self):
        with self.assertRaises(screener.UserError) as cm:
            self.call(self.cfg(self.cfgtext.replace('share_publish=1', 'share_publish=1\nmode=watchlist')))
        self.assertEqual(cm.exception.code, 15)
        self.assertIn('mode=full', cm.exception.cause)

    def test_leak_in_page_is_13(self):
        p = os.path.join(self.out, '分享版_20261022.html')
        tu.write(p, tu.readb(p).decode('utf-8').replace('<main>', '<main><p>1,000,000</p>', 1))
        with self.assertRaises(screener.UserError) as cm:
            self.call(self.cfg(), publish=False, confirm_first_publish=REPO)
        self.assertEqual(cm.exception.code, 13)
        self.assertEqual(self.gh.calls, [])

    def test_capital_leak_is_found_even_when_page_was_made_elsewhere(self):
        p = os.path.join(self.out, '分享版_20261022.html')
        tu.write(p, tu.readb(p).decode('utf-8').replace('<main>', '<main><p>100萬</p>', 1))
        with self.assertRaises(screener.UserError) as cm:
            self.call(self.cfg())
        self.assertEqual(cm.exception.code, 13)

    def test_bad_confirm_argument(self):
        with self.assertRaises(screener.UserError) as cm:
            self.call(self.cfg(), publish=False, confirm_first_publish='not a repo')
        self.assertEqual(cm.exception.code, 15)

    def test_argparse_accepts_new_subcommand(self):
        r = subprocess.run([sys.executable, '-B', os.path.join(ROOT, 'screener.py'), 'share', '--help'], capture_output=True,
                           timeout=60, env=tu.clean_env(PYTHONUTF8='1'))
        out = r.stdout.decode('utf-8')
        self.assertEqual(r.returncode, 0)
        for s in ('--publish', '--confirm-first-publish', '--takedown', '--ask'):
            self.assertIn(s, out)


# ---------- run.bat ----------
@unittest.skipUnless(os.name == 'nt', '需要 Windows')
class TestRunBatShare(tu._SetupRunner):
    BAT = 'run.bat'

    def setUp(self):
        super().setUp()
        tu.write(os.path.join(self.app, 'screener.py'),
                 'import os, sys\n'
                 'with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "argv.log"), "a", encoding="utf-8") as f:\n'
                 '    f.write(" ".join(sys.argv[1:]) + "\\n")\n'
                 'sys.exit(int(os.environ.get("FAKE_SHARE_RC", "0")) if sys.argv[1] == "share" '
                 'else int(os.environ.get("FAKE_SCAN_RC", "0")))\n')
        tu.write(os.path.join(self.app, 'cache', 'latest.txt'), '20261022\n')
        self.ps = os.path.join(self.sys32, 'WindowsPowerShell', 'v1.0')

    def go(self, cfg=True, nopause=True, **env):
        if cfg:
            tu.write(os.path.join(self.app, '設定.txt'), 'capital=0\n')
        base = dict(TWSTOCK_TODAY='20261022', TWSTOCK_DOW='4', TWSTOCK_PY=sys.executable, PATH=self.sys32 + ';' + self.ps)
        base.update(env)
        rc, out = self.run_bat([], nopause=nopause, **base)
        p = os.path.join(self.app, 'argv.log')
        lines = tu.readb(p).decode('utf-8').splitlines() if os.path.exists(p) else []
        return rc, out, lines

    def test_rc0_done_and_no_ask_under_nopause(self):
        rc, out, lines = self.go()
        self.assertEqual(rc, 0, out)
        self.assertIn('完成！結果頁已經用瀏覽器打開', out)
        self.assertEqual([l.split()[0] for l in lines], ['scan', 'share'])
        self.assertIn('share --config', lines[1])
        self.assertIn('--publish', lines[1])
        self.assertNotIn('--ask', lines[1])

    def test_rc10_is_wait_not_failure(self):
        rc, out, lines = self.go(FAKE_SHARE_RC='10')
        self.assertEqual(rc, 0, out)
        self.assertIn('分享版還沒有公開', out)
        self.assertIn('share_publish 改成 0', out)
        self.assertNotIn('沒有完成（代碼', out)
        self.assertNotIn('只有「公開分享版」沒有完成', out)

    def test_rc13_is_share_fail_with_code(self):
        rc, out, lines = self.go(FAKE_SHARE_RC='13')
        self.assertEqual(rc, 13, out)
        self.assertIn('只有「公開分享版」沒有完成（代碼 13）', out)
        self.assertIn('今天的選股結果不受影響', out)
        self.assertNotIn('沒有完成（代碼 13）。原因和建議做法', out)
        self.assertNotIn('完成！結果頁已經用瀏覽器打開', out)

    def test_log_line_written(self):
        rc, out, lines = self.go(FAKE_SHARE_RC='12')
        self.assertEqual(rc, 12, out)
        log = tu.readb(os.path.join(self.app, 'logs', 'run_20261022.log')).decode('utf-8', 'replace')
        self.assertIn('分享版公開步驟結束 代碼 12', log)

    def test_not_nopause_passes_ask(self):
        rc, out, lines = self.go(nopause=False)
        self.assertEqual(rc, 0, out)
        self.assertIn('--ask', lines[1])

    def test_no_config_no_share(self):
        rc, out, lines = self.go(cfg=False)
        self.assertEqual(rc, 0, out)
        self.assertEqual([l.split()[0] for l in lines], ['scan'])

    def test_scan_failure_skips_share(self):
        rc, out, lines = self.go(FAKE_SCAN_RC='9')
        self.assertEqual(rc, 9, out)
        self.assertEqual([l.split()[0] for l in lines], ['scan'])


class TestShareStatic(unittest.TestCase):
    def raw(self, name):
        return tu.readb(os.path.join(ROOT, name))

    def bat_code(self):
        t = self.raw('run.bat').decode('utf-8')
        return t.split(tu.MARK.decode('ascii'))[0]

    def test_run_bat_has_share_lines_and_never_calls_gh(self):
        code = self.bat_code()
        self.assertIn('screener.py share --config "%F_CFG%" --publish %SHARE_ASK%', code)
        self.assertIn('TWSTOCK_NOPAUSE', code)
        for ln in code.splitlines():
            self.assertIsNone(re.search(r'\bgh(\.exe)?\b', ln, re.I), ln)
            self.assertIsNone(re.search(r'curl|certutil|bitsadmin|https?://', ln, re.I), ln)
        code.encode('ascii')
        msgs = tu.bat_msgs('run.bat')
        for k in ('SHARE_WAIT', 'SHARE_FAIL', 'LOGSHARE'):
            self.assertIn(k, msgs)
            for line in msgs[k]:
                self.assertNotRegex(line, r'["^|]|https?:')
        self.assertIn('!RC!', ''.join(msgs['SHARE_FAIL']))
        self.assertIn('!RC!', ''.join(msgs['LOGSHARE']))

    def test_no_hardcoded_personal_account(self):
        for name in ('screener.py', 'share.py', 'run.bat', '設定.範例.txt', 'run_daily.bat', 'setup.bat'):
            self.assertNotIn(b'leohsiehtw', self.raw(name).lower(), name)
            self.assertNotIn(b'isleohsieh', self.raw(name).lower(), name)

    def test_line_endings(self):
        for name, crlf in (('share.py', True), ('screener.py', True), ('設定.範例.txt', True), ('run.bat', True),
                           (os.path.join('tests', 'test_share.py'), False), ('README.md', False), ('CHANGELOG.md', False)):
            b = self.raw(name)
            self.assertFalse(b.startswith(b'\xef\xbb\xbf'), name)
            if crlf:
                self.assertEqual(b.count(b'\r\n'), b.count(b'\n'), name)
            else:
                self.assertEqual(b.count(b'\r'), 0, name)

    def test_share_py_is_standalone_and_has_no_shell(self):
        t = self.raw('share.py').decode('utf-8')
        self.assertNotRegex(t, r'(?m)^\s*(import|from)\s+screener')
        self.assertNotIn('shell=True', t)
        self.assertNotIn('os.system', t)
        self.assertNotIn('auth token', t)
        self.assertNotIn('urllib', t)

    def test_exit_codes_documented(self):
        self.assertEqual((share.E_CONFIRM, share.E_GH_MISSING, share.E_GH_AUTH, share.E_LEAK, share.E_GITHUB, share.E_SHARE),
                         (10, 11, 12, 13, 14, 15))
        self.assertEqual(share.MAX_BYTES, 300000)
        self.assertIn('10～15', screener.__doc__)

    def test_gitignore_keeps_personal_files_out(self):
        t = self.raw('.gitignore').decode('utf-8')
        self.assertIn('cache/', t)
        self.assertIn('結果/', t)


# ---------- 類股在分享版 ----------
def sec_page(sectors, status='ok', fetched='2026-10-20'):
    res = tu.fake_results()
    for r in res:
        if 'date' in r:
            r['date'] = '2026-10-22'
        r['sector'] = sectors.get(r['code'], '')
    return screener.render_share_html(res, {'status': status, 'fetched': fetched})


class TestShareSector(unittest.TestCase):
    def rules(self, **k):
        d = dict(capital=1000000, paths=[ROOT], sectors=screener.SECTOR_LABELS)
        d.update(k)
        return share.make_rules(**d)

    def test_page_structure_and_scripts(self):
        page = sec_page({'1101': '傳產-水泥', '2330': '科技-半導體', '00646': 'ETF-市值型'})
        self.assertEqual(share.check_html(page.encode('utf-8'), self.rules()), [])
        head = page[:page.index('</head>')]
        self.assertEqual((page.count('<script'), head.count('<script')), (2, 1))
        for s in ('id="csec"', 'data-sec="科技-半導體"', 'data-set="dark"', 'aria-pressed="true"', 'role="group"', '類股：'):
            self.assertIn(s, page, s)
        self.assertIn('csec', screener.SHARE_JS)
        self.assertIn('twss-theme', screener.SHARE_JS)
        self.assertIn('setTheme', screener.SHARE_JS)

    def test_structure_violations(self):
        page = sec_page({'1101': '傳產-水泥'})
        boot = '<script>' + screener.THEME_BOOT + '</script>\n'
        self.assertIn(boot, page)
        cases = {
            'head script moved to body': page.replace(boot, '', 1).replace('</body>', boot + '</body>'),
            'third script': page.replace('</body>', '<script>var a=1;</script></body>'),
            'no head script': page.replace(boot, '', 1),
            'aria-pressed bad': page.replace('data-set="auto" aria-pressed="true"', 'data-set="auto" aria-pressed="yes"', 1),
            'data-set bad': page.replace('data-set="auto"', 'data-set="x"', 1),
            'data-sec path': page.replace('data-sec="傳產-水泥"', 'data-sec="C:/x"', 1),
            'data-sec name': page.replace('data-sec="傳產-水泥"', 'data-sec="鴻海"', 1),
            'data-sec html': page.replace('data-sec="傳產-水泥"', 'data-sec="a&lt;b"', 1),
        }
        for name, bad in cases.items():
            with self.subTest(name):
                self.assertTrue(share.check_html(bad.encode('utf-8'), self.rules()), name)
        fmt_only = page.replace('data-sec="傳產-水泥"', 'data-sec="鴻海"', 1).encode('utf-8')
        self.assertEqual(share.check_html(fmt_only, share.make_rules(capital=1000000, paths=[ROOT])), [])    # 沒給清單時只看格式

    def test_every_label_page_under_limit(self):
        res = []
        for i, label in enumerate(screener.SECTOR_ORDER):
            r = tu.fake_rec('%04d' % (3000 + i), '測試%s' % '甲乙丙丁戊己庚辛壬癸'[i % 10], 100.0 + i, 1.0, 2.0, '突破型', 95.0 + i)
            r['sector'] = label
            res.append(r)
        page = screener.render_share_html(res, {'status': 'ok', 'fetched': '2026-10-20'}).encode('utf-8')
        self.assertLess(len(page), share.MAX_BYTES)
        self.assertEqual(share.check_html(page, self.rules()), [])
        for label in screener.SECTOR_ORDER:
            self.assertIn(('data-sec="%s"' % label).encode('utf-8'), page)

    def test_watchlist_name_equal_to_label_word(self):
        wl = {'1234': '半導體'}
        ok = sec_page({'1101': '科技-半導體'}).encode('utf-8')
        self.assertEqual(share.check_html(ok, self.rules(watchlist=wl)), [])
        bad = sec_page({'1101': '科技-半導體'}).replace('<main>', '<main><p>半導體</p>', 1).encode('utf-8')
        self.assertTrue(share.check_html(bad, self.rules(watchlist=wl)))
        bad2 = sec_page({'1101': '航運'}).replace('<main>', '<main><p>長榮航運</p>', 1).encode('utf-8')
        self.assertTrue(share.check_html(bad2, self.rules(watchlist={'1234': '長榮航運'})))
        bad3 = sec_page({}).replace('<main>', '<main><p>半導體</p>', 1).encode('utf-8')
        self.assertTrue(share.check_html(bad3, self.rules(watchlist=wl)))

    def test_outside_labels(self):
        f = share._outside_labels
        words = {'科技-半導體', '科技', '航運'}
        self.assertFalse(f('x 科技-半導體 y', '半導體', words))
        self.assertTrue(f('x 科技-半導體 半導體', '半導體', words))
        self.assertTrue(f('長榮航運', '長榮航運', words))
        self.assertFalse(f('<p>航運</p>', '航運', words))
        self.assertTrue(f('abc 鴻海', '鴻海', words))
        self.assertFalse(f('abc', '鴻海', words))

    def test_share_rows_sector_validation(self):
        res = tu.fake_results()
        for bad in ('鴻海', 5, None, '科技-半導體 ', 'ETF'):
            res[0]['sector'] = bad
            with self.assertRaises(ValueError, msg=repr(bad)):
                screener.share_rows(res)
        res[0]['sector'] = '航運'
        self.assertIn('航運', [r['sector'] for r in screener.share_rows(res)])
        del res[0]['sector']
        self.assertTrue(all(r['sector'] == '' for r in screener.share_rows(res)))
        with self.assertRaises(ValueError):
            screener.render_share_html(res, {'status': 'weird', 'fetched': ''})
        with self.assertRaises(ValueError):
            screener.render_share_html(res, {'status': 'ok', 'fetched': '2026/10/20'})
        self.assertEqual(screener.render_share_html(res, None),
                         screener.render_share_html(res, {'status': 'none', 'fetched': ''}))

    def test_stale_note_in_share(self):
        page = sec_page({'1101': '傳產-水泥'}, status='stale', fetched='2026-09-01')
        self.assertIn('類股資料未更新（上次更新 2026-09-01）', page)
        self.assertEqual(share.check_html(page.encode('utf-8'), self.rules()), [])

    def test_python39_compatible(self):
        import ast
        for f in ('screener.py', 'share.py', 'fetch_data.py'):
            with open(os.path.join(ROOT, f), encoding='utf-8') as fh:
                src = fh.read()
            ast.parse(src, feature_version=(3, 9))
        with open(os.path.join(ROOT, 'share.py'), encoding='utf-8') as fh:
            src = fh.read()
        self.assertNotIn(']++', src)             # 佔有量詞要 Python 3.11 才有
        self.assertNotIn('*+檔', src)

    def test_adversarial_mask_is_fast(self):
        import time
        for text in ('1' * 300000, '1 ' * 150000, '1' * 150000 + ' ' * 150000, ('1' + ' ' * 50) * 5000 + 'x'):
            t0 = time.time()
            share._mask_watch_text(text, [], [])
            self.assertLess(time.time() - t0, 3, text[:10])


if __name__ == '__main__':
    unittest.main()
