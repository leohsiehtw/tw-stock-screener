"""分享頁的每日歷史與 PWA（設定檔 share_pwa=1）：頁面、歷史日檔、檢查器、公開流程（gh 全部用假的）、JS／CSS 靜態檢查。

不連網、不執行真的 gh、不碰使用者的 data／cache／結果：全部用暫存資料夾。執行：python -m unittest discover -s tests -v
"""
import argparse, datetime as dt, hashlib, json, os, re, shutil, struct, sys, unittest, zlib
from unittest import mock

import test_usability as tu
import test_share as ts          # 只拿它的假 gh 和輔助函式；不要用 from ... import 把測試類別拉進來

screener, ROOT = tu.screener, tu.ROOT
import share  # noqa: E402

REPO, LOGIN = ts.REPO, ts.LOGIN
RULES_KW = dict(capital=1000000, paths=[ROOT, r'C:\Users\lh'], watchlist={'2454': '聯發科', '1216': '統一'},
                repo='someowner/my-results', people=('someuser', 'SOMEPC'), sectors=screener.SECTOR_LABELS)


def rules():
    return share.make_rules(**RULES_KW)


def res_for(date, tweak=None):
    res = tu.fake_results()
    for r in res:
        if 'date' in r:
            r['date'] = date
    if tweak:
        tweak(res)
    return res


def day_bytes(date, prev_ks=None, tweak=None):
    res = res_for(date, tweak)
    ok = [r for r in res if 'error' not in r]
    return screener.history_json(screener.share_rows(res), date, len(res), len(ok), prev_ks or {})


def pwa_page(date='2026-10-22', charts=None):
    return screener.render_share_html(res_for(date), None, charts, True)


def hist_obj(date='2026-10-22'):
    return json.loads(day_bytes(date).decode('utf-8'))


def dump(obj):
    return json.dumps(obj, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


# ---------- 黃金雜湊：share_pwa 關閉時，所有頁面逐位元組不變 ----------
class TestGolden(unittest.TestCase):
    H = staticmethod(lambda t: hashlib.sha256(t.encode('utf-8')).hexdigest())

    def test_pwa_off_pages_are_byte_identical_to_before(self):
        self.assertEqual(self.H(screener.render_share_html(tu.fake_results())),
                         '259f80ac7359eda09389753884175377e07d7bfaa147b8a84955f00bc2e7459a')
        charts = {c: screener.chart_code(screener.chart_series(tu.rw_bars(150, i + 1)))
                  for i, c in enumerate(('1101', '2330', '00646', '6874', '2454'))}
        self.assertEqual(self.H(screener.render_share_html(tu.fake_results(), None, charts)),
                         'd0179185c1d2bd73a0902c81b50b5c92b4e87d3bc6fe6a857a645936e836a52b')
        self.assertEqual(self.H(screener.render_html(tu.fake_results(), False, tu.meta_for())),
                         'a9b061cfc991d02437cae865c025c79e3a0e63621ad0e8fb436c2716b73495be')

    def test_pwa_page_needs_pwa_flag(self):
        page = pwa_page().encode('utf-8')
        self.assertEqual(share.check_html(page, rules(), pwa=True), [])
        self.assertTrue(share.check_html(page, rules()))
        self.assertTrue(share.check_html(page, rules(), pwa=False))

    def test_pwa_off_page_is_not_a_pwa_page(self):
        plain = screener.render_share_html(tu.fake_results()).encode('utf-8')
        self.assertTrue(share.check_html(plain, rules(), pwa=True))        # 缺少 PWA 的 link、日期列…
        for s in ('manifest', 'hbar', 'bnav', 'serviceWorker', 'viewport-fit'):
            self.assertNotIn(s, plain.decode('utf-8'))

    def test_pwa_page_structure(self):
        h = pwa_page()
        self.assertEqual(h.count('<script'), 2)
        self.assertEqual(h.count('<style>'), 1)
        for s in ('<link rel="manifest" href="manifest.webmanifest">', 'id="hbar" data-day="2026-10-22" hidden', 'id="hview"',
                  '<section class="band alt" id="hist"', 'id="htrend"', '<nav class="bnav" id="bnav"', 'id="a2hs"',
                  'viewport-fit=cover', 'content="#ffffff" media="(prefers-color-scheme: light)"', 'href="#hist"', '歷史紀錄：'):
            self.assertIn(s, h, s)
        self.assertEqual(h.count('<a href="#cands">今日</a>'), 1)
        self.assertLess(len(h.encode('utf-8')), 300_000)

    def test_chart_budget_still_holds_with_pwa(self):
        res = [tu.fake_rec('%d' % (1100 + i), '標的%d' % i, 50 + i, 0.5, 1 + i / 100, '回測型', 45 + i) for i in range(150)]
        charts = {r['code']: ('abcdefgh' * 250) for r in res}
        h = screener.render_share_html(res, None, charts, True)
        self.assertTrue(0 < h.count(' data-ch="') < 150)
        self.assertLess(len(h.encode('utf-8')), screener.SHARE_CHART_BUDGET)
        self.assertEqual(share.check_html(h.encode('utf-8'), share.make_rules(capital=1000000, paths=[ROOT],
                                                                              sectors=screener.SECTOR_LABELS), pwa=True), [])

    def test_theme_colors_match_palette(self):
        self.assertEqual(share.THEME_LIGHT, screener.PALETTE['light']['page'])
        self.assertEqual(share.THEME_DARK, screener.PALETTE['dark']['page'])
        self.assertIn('content="%s" media="(prefers-color-scheme: light)"' % screener.PALETTE['light']['page'], share.PWA_HEAD_TAGS)
        self.assertIn('content="%s" media="(prefers-color-scheme: dark)"' % screener.PALETTE['dark']['page'], share.PWA_HEAD_TAGS)
        self.assertEqual(json.loads(share.manifest_bytes())['theme_color'], share.THEME_LIGHT)


# ---------- 頁面結構的負面測試 ----------
class TestPwaPageNegatives(unittest.TestCase):
    page = pwa_page()

    def check(self, h):
        return share.check_html(h.encode('utf-8'), rules(), pwa=True)

    def test_base_page_passes(self):
        self.assertEqual(self.check(self.page), [])

    def swap(self, old, new, count=1):
        self.assertIn(old, self.page, old)
        return self.page.replace(old, new, count)

    def test_links(self):
        m = '<link rel="manifest" href="manifest.webmanifest">'
        bad = [self.swap(m, '<link rel="manifest" href="https://x.example/m.webmanifest">'),
               self.swap(m, '<link rel="manifest" href="/manifest.webmanifest">'),
               self.swap(m, '<link rel="manifest" href="//cdn.example/m">'),
               self.swap(m, m + '<link rel="stylesheet" href="a.css">'),
               self.swap(m, m + '<link rel="manifest" href="manifest.webmanifest">'),
               self.swap(m, m + '<link rel="preload" href="a.js">'),
               self.swap('<main>', '<main>' + m),                                   # link 在 body
               self.swap('<main>', '<main><link rel="icon" href="icons/icon.svg" type="image/svg+xml">'),
               self.swap(m, m.replace('manifest.webmanifest', 'manifest.json'))]
        for h in bad:
            self.assertTrue(self.check(h), h[:0])

    def test_meta(self):
        for old, new in (('content="#000000"', 'content="#111111"'), ('content="#ffffff" media', 'content="#fafafa" media'),
                         ('(prefers-color-scheme: dark)', '(prefers-color-scheme: light)'),
                         ('initial-scale=1, viewport-fit=cover', 'initial-scale=1, user-scalable=no'),
                         ('content="台股選股"', 'content="別的名字"')):
            self.assertTrue(self.check(self.swap(old, new)), old)
        theme = '<meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">'
        self.assertTrue(self.check(self.swap(theme, theme + theme)))
        self.assertTrue(self.check(self.swap(theme, '')))

    def test_data_day_and_ids(self):
        for new in ('data-day="2026-10-2"', 'data-day="x"', 'data-day="2026-10-22 "'):
            self.assertTrue(self.check(self.swap('data-day="2026-10-22"', new)), new)
        self.assertTrue(self.check(self.swap('<main>', '<main><div data-day="2026-10-22"></div>')))
        self.assertTrue(self.check(self.swap('<main>', '<main><div id="zzz"></div>')))
        self.assertTrue(self.check(self.swap('<main>', '<main><div id="hbar"></div>')))              # 重複的 id
        self.assertTrue(self.check(self.swap('</body>', '<script>x</script></body>')))               # 第三個 script
        self.assertTrue(self.check(self.swap('<main>', '<main><svg></svg>')))
        self.assertTrue(self.check(self.swap('<main>', '<main><a href="history/20261022.json">x</a>')))
        self.assertTrue(self.check(self.swap('<main>', '<main><p onclick="x">x</p>')))
        self.assertTrue(self.check(self.swap('<main>', '<main><p media="x">x</p>')))

    def test_missing_parts(self):
        self.assertTrue(self.check(self.swap('<nav class="bnav" id="bnav"', '<nav class="bnav" id="bnav2"')))
        self.assertTrue(self.check(self.swap('id="hbar"', 'id="hist-h"')))
        for tail in ('href="https://x.example/"', 'a@b.example'):
            self.assertTrue(self.check(self.swap('<main>', '<main><p>%s</p>' % tail)))

    def test_leaks_still_caught(self):
        for t in ('1,000,000', '<p>C:\\Users\\someuser\\x</p>', 'share=1', '風險', 'someowner/my-results', '聯發科 2454'):
            self.assertTrue(self.check(self.swap('<main>', '<main><p>%s</p>' % t)), t)


# ---------- 歷史日檔 ----------
class TestCheckHistory(unittest.TestCase):
    def check(self, data, tag='20261022', r=None):
        return share.check_history(data, r or rules(), tag)

    def test_good_file(self):
        b = day_bytes('2026-10-22')
        self.assertEqual(self.check(b), [])
        o = json.loads(b)
        self.assertEqual((o['v'], o['date'], o['n'], o['nb'], o['np'], len(o['rows'])), (1, '2026-10-22', 4, 1, 3, 4))
        self.assertEqual(list(o), ['v', 'date', 'scanned', 'valid', 'n', 'nb', 'np', 'rows'])
        self.assertEqual([r for r in o['rows'] if r[0] == '1101'][0], ['1101', '台泥', '突破型', '120.0', '3.1', '2.4', '117.0', '', '', '1'])
        self.assertTrue(all(isinstance(x, str) for r in o['rows'] for x in r))
        self.assertEqual(b, dump(o))

    def mut(self, fn):
        o = hist_obj()
        fn(o)
        return dump(o)

    def test_structure_negatives(self):
        cases = {
            'extra key': lambda o: o.update(x=1),
            'missing key': lambda o: o.pop('np'),
            'wrong order': lambda o: o.update(o.pop('date') and {}),
            'version': lambda o: o.update(v=2),
            'bad date': lambda o: o.update(date='2026-13-40'),
            'other day': lambda o: o.update(date='2026-10-21'),
            'number row': lambda o: o['rows'].__setitem__(0, [1101, '台泥', '突破型', 120.0, 3.1, 2.4, 117.0, '', '', 1]),
            'short row': lambda o: o['rows'][0].pop(),
            'long row': lambda o: o['rows'][0].append('x'),
            'exponent': lambda o: o['rows'][0].__setitem__(3, '1e5'),
            'nan string': lambda o: o['rows'][0].__setitem__(3, 'nan'),
            'zero close': lambda o: o['rows'][0].__setitem__(3, '0.0'),
            'negative stop': lambda o: o['rows'][0].__setitem__(6, '-1.0'),
            'five decimals': lambda o: o['rows'][0].__setitem__(4, '1.12345'),
            'dup code': lambda o: o['rows'].append(list(o['rows'][0])),
            'n mismatch': lambda o: o.update(n=99),
            'nb mismatch': lambda o: o.update(nb=3),
            'np mismatch': lambda o: o.update(np=0),
            'valid < n': lambda o: o.update(valid=1),
            'scanned < valid': lambda o: o.update(scanned=5),
            'bool int': lambda o: o.update(n=True),
            'bad type': lambda o: o['rows'][0].__setitem__(2, '別型'),
            'bad market': lambda o: o['rows'][0].__setitem__(8, '興櫃'),
            'bad sector': lambda o: o['rows'][0].__setitem__(7, '不存在的類股'),
            'k zero': lambda o: o['rows'][0].__setitem__(9, '0'),
            'bad code': lambda o: o['rows'][0].__setitem__(0, 'ab'),
        }
        for name, fn in cases.items():
            self.assertTrue(self.check(self.mut(fn)), name)

    def test_nonstandard_encoding(self):
        b = day_bytes('2026-10-22')
        self.assertTrue(self.check(b.replace(b'"120.0"', b'NaN')))
        self.assertTrue(self.check(b + b'\n'))
        self.assertTrue(self.check(b.replace(b',', b', ', 1)))
        self.assertTrue(self.check(b'\xef\xbb\xbf' + b))
        self.assertTrue(self.check(json.dumps(hist_obj(), ensure_ascii=True).encode()))
        self.assertTrue(self.check(b'{"v":1,"v":1}'))
        self.assertTrue(self.check(b'\xff\xfe'))
        self.assertTrue(self.check(b'[]'))

    def test_leaks_in_names(self):
        for name in ('總資金 1000000', '1000000', 'C:\\Users\\x', 'https://a.example', '風險', '部位', '自選股', 'a\u200bb',
                     'someowner/my-results', 'x@y.example', '2454', '聯發科 1216'):
            o = hist_obj()
            o['rows'][1][1] = name
            r = self.check(dump(o))
            self.assertTrue(r, name)
            if name not in share.FORBIDDEN_WORDS:
                self.assertNotIn(name, '；'.join(r), name)

    def test_size_limit(self):
        o = hist_obj()
        row = o['rows'][0]
        o['rows'] = [[('%04d' % (1000 + i)), row[1]] + row[2:] for i in range(700)]
        b = dump(o)
        self.assertGreater(len(b), 40_000)
        self.assertTrue([p for p in self.check(b) if '太大' in p])

    def test_scanned_equal_to_watchlist_code_is_not_blocked(self):
        o = hist_obj()
        o['scanned'], o['valid'] = 2454, 2454
        self.assertEqual(self.check(dump(o)), [])
        o['scanned'], o['valid'] = 2454, 1216
        self.assertEqual(self.check(dump(o)), [])

    def test_watchlist_stock_that_is_a_candidate_is_fine(self):
        r = share.make_rules(**dict(RULES_KW, watchlist={'2330': '台積電', '1101': '台泥'}))
        self.assertEqual(self.check(day_bytes('2026-10-22'), r=r), [])

    def test_hist_index(self):
        good = dump(dict(v=1, days=[dict(d='2026-10-22', n=4, b=1), dict(d='2026-10-21', n=3, b=0)]))
        self.assertEqual(share.check_hist_index(good), [])
        for bad in (dict(v=1, days=[dict(d='2026-10-21', n=4, b=1), dict(d='2026-10-22', n=3, b=0)]),
                    dict(v=1, days=[dict(d='2026-10-22', n=4, b=5)]),
                    dict(v=1, days=[dict(d='2026-10-22', n=4, b=1, x=1)]),
                    dict(v=1, days=[dict(d='2026-10-22', n=4, b=1)] * 2),
                    dict(v=2, days=[]), dict(v=1), dict(v=1, days=[dict(d='x', n=1, b=1)]),
                    dict(v=1, days=[dict(d=(dt.date(2026, 1, 1) + dt.timedelta(days=-i)).isoformat(), n=1, b=0) for i in range(121)])):
            self.assertTrue(share.check_hist_index(dump(bad)), str(bad)[:60])
        self.assertTrue(share.check_hist_index(good + b' '))
        self.assertLessEqual(len(dump(dict(v=1, days=[dict(d='2026-10-22', n=999, b=999)] * 1)) * 120), 120 * 100)


# ---------- manifest、SW、圖示、路徑 ----------
class TestPwaFiles(unittest.TestCase):
    def test_manifest(self):
        b = share.manifest_bytes()
        self.assertEqual(share.check_manifest(b, rules()), [])
        o = json.loads(b)
        self.assertEqual((o['name'], o['short_name'], o['start_url'], o['scope'], o['id'], o['display']),
                         ('台股盤後選股', '台股選股', './', './', './', 'standalone'))
        self.assertEqual({i['src'] for i in o['icons']}, set(share.ICON_SRCS))
        self.assertTrue(any(i['purpose'] == 'maskable' for i in o['icons']))
        for fn in (lambda x: x.update(extra='x'), lambda x: x.update(start_url='/'), lambda x: x.update(scope='/x/'),
                   lambda x: x.update(id='/'), lambda x: x.update(display='browser'), lambda x: x.update(theme_color='red'),
                   lambda x: x['icons'].append(dict(src='https://x.example/i.png', sizes='1x1', type='image/png')),
                   lambda x: x['icons'][0].update(src='icons/evil.png'), lambda x: x.pop('icons'),
                   lambda x: x.update(name='總資金')):
            o = json.loads(b)
            fn(o)
            self.assertTrue(share.check_manifest(dump(o), rules()))
        self.assertTrue(share.check_manifest(b + b'\n'))

    def test_sw_is_exact(self):
        sw = share.sw_bytes('20261022-0123abcd', ['history/20261022.json', 'history/20261021.json'])
        self.assertEqual(share.check_sw(sw), [])
        self.assertTrue(share.check_sw(sw + b' '))
        for i in (0, 40, len(sw) // 2, len(sw) - 2):
            self.assertTrue(share.check_sw(sw[:i] + bytes([sw[i] ^ 1]) + sw[i + 1:]), i)
        self.assertTrue(share.check_sw(sw.replace(b'3e3', b'4e3')))
        self.assertTrue(share.check_sw(b'x'))
        self.assertIn(b"twss-shell-", sw)
        self.assertEqual(share.check_sw(sw.replace(b'20261022-0123abcd', b'20261022-0123abcd')), [])
        with self.assertRaises(AssertionError):
            share.sw_bytes('bad ver', [])
        with self.assertRaises(AssertionError):
            share.sw_bytes('20261022-0123abcd', ['../x.json'])

    def test_icons(self):
        for size, mask in ((192, False), (512, False), (512, True), (180, False)):
            b = share.png_icon(size, mask)
            self.assertEqual(share.check_png(b, size), [], (size, mask))
            self.assertLess(len(b), 64_000)
            self.assertEqual(b, share.png_icon(size, mask))
        self.assertNotEqual(share.png_icon(512), share.png_icon(512, True))
        self.assertEqual(share.check_svg(share.ICON_SVG), [])
        self.assertTrue(share.check_svg(share.ICON_SVG + b' '))
        self.assertIn(b'http://www.w3.org/2000/svg', share.ICON_SVG)
        self.assertLess(len(share.ICON_SVG), 4_000)

    def pixels(self, b, size):
        raw = zlib.decompress(b[b.index(b'IDAT') + 4:b.index(b'IEND') - 8])
        stride = 1 + size * 3
        return lambda x, y: raw[y * stride + 1 + x * 3: y * stride + 4 + x * 3]

    def test_icon_look(self):
        px = self.pixels(share.png_icon(192), 192)
        self.assertEqual(px(2, 2), bytes((0x00, 0x71, 0xe3)))                  # 藍底
        whites = sum(1 for y in range(192) for x in range(192) if px(x, y) == b'\xff\xff\xff')
        self.assertGreater(whites, 192 * 192 * 0.08)
        mpx = self.pixels(share.png_icon(512, True), 512)                      # maskable：四個角和邊緣的 10% 內都是底色
        for y in range(512):
            for x in range(512):
                if x < 51 or y < 51 or x >= 461 or y >= 461:
                    self.assertEqual(mpx(x, y), bytes((0x00, 0x71, 0xe3)))
                    break

    def test_png_negatives(self):
        good = share.png_icon(192)
        chunk = share._png_chunk
        text = good[:-12] + chunk(b'tEXt', b'k\x00v') + good[-12:]
        bad_crc = bytearray(good)
        bad_crc[40] ^= 1
        for name, b, size in (('tEXt', text, 192), ('crc', bytes(bad_crc), 192), ('size', good, 193), ('trailing', good + b'x', 192),
                              ('not png', b'GIF89a', 192), ('truncated', good[:-5], 192), ('empty', b'', 192),
                              ('big', good + b'\0' * 64_000, 192)):
            self.assertTrue(share.check_png(b, size), name)
        ihdr = struct.pack('>IIBBBBB', 192, 192, 8, 6, 0, 0, 0)               # RGBA
        rgba = share.PNG_SIG + chunk(b'IHDR', ihdr) + chunk(b'IDAT', zlib.compress(b'\0' * 100)) + chunk(b'IEND', b'')
        self.assertTrue(share.check_png(rgba, 192))

    def test_paths(self):
        for p in ('index.html', 'README.md', 'sw.js', 'manifest.webmanifest', 'icons/icon.svg', 'icons/maskable-512.png',
                  'history/index.json', 'history/20261022.json'):
            self.assertTrue(share.is_pwa_path(p), p)
        for p in ('history/../x.json', 'History/20261022.json', 'history/2026102.json', 'history/202610222.json', 'icons/evil.js',
                  'a\\b', '/sw.js', 'secret.txt', 'history/x.txt', 'history/20261022.json/', 'history//20261022.json',
                  'icons/icon.svg ', 'SW.js', '', 'history/index.json.bak', 'history/20261022.JSON'):
            self.assertFalse(share.is_pwa_path(p), p)


# ---------- 歷史紀錄的寫入（update_share_history）----------
class HistCase(tu.TempCase):
    def setUp(self):
        super().setUp()
        self.hist = os.path.join(self.tmp, 'cache', 'share', 'history')
        self.data = os.path.join(self.tmp, 'data')
        self.out = os.path.join(self.tmp, '結果')
        os.makedirs(self.out)
        self.days = ['20261019', '20261020', '20261021', '20261022', '20261023']
        for d in self.days:
            tu.write(os.path.join(self.tmp, 'cache', 'twse', d + '.json'), '{}')
        self.rules = share.make_rules(capital=1000000, paths=[self.tmp], sectors=screener.SECTOR_LABELS)

    def upd(self, date, tweak=None):
        return tu.quiet(screener.update_share_history, self.hist, res_for(date, tweak), self.data, self.out, self.rules)

    def read(self, tag):
        return json.loads(tu.readb(os.path.join(self.hist, tag + '.json')))

    def ks(self, tag):
        return {r[0]: r[9] for r in self.read(tag)['rows']}


class TestHistory(HistCase):
    def test_same_data_twice_does_not_touch_the_file(self):
        self.assertTrue(self.upd('2026-10-22')[0])
        p = os.path.join(self.hist, '20261022.json')
        first = tu.readb(p)
        os.utime(p, ns=(10 ** 9, 10 ** 9))
        self.assertTrue(self.upd('2026-10-22')[0])
        self.assertEqual(tu.readb(p), first)
        self.assertEqual(os.stat(p).st_mtime_ns, 10 ** 9)
        self.assertEqual([n for n in os.listdir(self.hist) if n.endswith('.tmp')], [])

    def test_rerun_on_a_holiday_changes_nothing_and_new_day_adds_one(self):
        self.upd('2026-10-21')
        self.upd('2026-10-22')
        before = {n: tu.readb(os.path.join(self.hist, n)) for n in os.listdir(self.hist)}
        self.upd('2026-10-22')                                            # 休市日：掃到的還是上一個交易日的資料
        self.assertEqual(before, {n: tu.readb(os.path.join(self.hist, n)) for n in os.listdir(self.hist)})
        self.upd('2026-10-23')
        self.assertEqual(sorted(os.listdir(self.hist)), ['20261021.json', '20261022.json', '20261023.json'])
        self.assertEqual(len(os.listdir(self.hist)), len(before) + 1)

    def test_a_new_day_with_one_more_candidate(self):
        self.upd('2026-10-22')
        extra = lambda res: res.insert(0, tu.fake_rec('3008', '大立光', 2500.0, 0.4, 1.9, '回測型', 2400.0))
        self.upd('2026-10-23', extra)
        self.assertEqual((self.read('20261022')['n'], self.read('20261023')['n']), (4, 5))

    def test_consecutive_days_counter(self):
        self.upd('2026-10-20')
        self.upd('2026-10-21')
        self.assertEqual(set(self.ks('20261020').values()), {'1'})
        self.assertEqual(set(self.ks('20261021').values()), {'2'})
        drop = lambda res: [res.remove(r) for r in list(res) if r['code'] == '2330']
        self.upd('2026-10-22', drop)
        k22 = self.ks('20261022')
        self.assertNotIn('2330', k22)
        self.assertEqual(k22['1101'], '3')
        self.upd('2026-10-23')
        k23 = self.ks('20261023')
        self.assertEqual(k23['2330'], '1')                                  # 前一個交易日沒有 → 重新算
        self.assertEqual(k23['1101'], '4')

    def test_gap_in_calendar_breaks_the_streak_only_when_a_trading_day_is_missing(self):
        self.upd('2026-10-19')
        os.remove(os.path.join(self.tmp, 'cache', 'twse', '20261020.json'))
        self.upd('2026-10-21')                                              # 前一個交易日是 19 號（20 號不在日曆裡）
        self.assertEqual(set(self.ks('20261021').values()), {'2'})

    def test_no_calendar_means_k_is_one(self):
        shutil.rmtree(os.path.join(self.tmp, 'cache', 'twse'))
        self.upd('2026-10-21')
        self.upd('2026-10-22')
        self.assertEqual(set(self.ks('20261022').values()), {'1'})

    def test_keeps_only_the_newest_120(self):
        os.makedirs(self.hist)
        start = dt.date(2026, 1, 1)
        names = [(start + dt.timedelta(days=i)).strftime('%Y%m%d') + '.json' for i in range(120)]
        for n in names:
            tu.write(os.path.join(self.hist, n), '{}')
        self.assertTrue(self.upd('2026-10-22')[0])
        left = sorted(os.listdir(self.hist))
        self.assertEqual(len(left), 120)
        self.assertNotIn(names[0], left)
        self.assertIn('20261022.json', left)
        self.assertIn(names[1], left)

    def test_backfill_only_when_folder_missing_and_only_days_with_share_page(self):
        for d, with_page in (('20261019', True), ('20261020', False), ('20261021', True)):
            res = res_for('%s-%s-%s' % (d[:4], d[4:6], d[6:]))
            tu.write(os.path.join(self.out, '盤後選股_%s.json' % d), json.dumps(res, ensure_ascii=False))
            if with_page:
                tu.write(os.path.join(self.out, '分享版_%s.html' % d), 'x')
        self.upd('2026-10-22')
        self.assertEqual(sorted(os.listdir(self.hist)), ['20261019.json', '20261021.json', '20261022.json'])
        self.assertEqual(self.ks('20261019')['1101'], '1')
        self.assertEqual(self.ks('20261021')['1101'], '1')                  # 前一個交易日 20 號沒有快照 → 重新算
        for n in os.listdir(self.hist):
            self.assertEqual(share.check_history(tu.readb(os.path.join(self.hist, n)), self.rules, n[:8]), [])
        os.remove(os.path.join(self.hist, '20261019.json'))
        self.upd('2026-10-23')
        self.assertNotIn('20261019.json', os.listdir(self.hist))

    def test_backfill_limit_and_bad_days_skipped(self):
        for i in range(125):
            d = (dt.date(2026, 5, 1) + dt.timedelta(days=i)).strftime('%Y%m%d')
            res = res_for('%s-%s-%s' % (d[:4], d[4:6], d[6:]))
            tu.write(os.path.join(self.out, '盤後選股_%s.json' % d), json.dumps(res, ensure_ascii=False))
            tu.write(os.path.join(self.out, '分享版_%s.html' % d), 'x')
        tu.write(os.path.join(self.out, '盤後選股_20261001.json'), 'not json')
        tu.write(os.path.join(self.out, '分享版_20261001.html'), 'x')
        self.upd('2026-10-22')
        n = len(os.listdir(self.hist))
        self.assertEqual(n, 120)

    def test_bad_data_writes_nothing_and_warns(self):
        def bad(res):
            res[0]['close'] = float('nan')
        ok, err = self.upd('2026-10-22', bad)
        self.assertFalse(ok)
        self.assertIn('今天的歷史紀錄沒有寫入', err)
        self.assertIn('分享版不受影響', err)
        self.assertFalse([n for n in os.listdir(self.hist) if n.startswith('20261022')] if os.path.isdir(self.hist) else [])

    def test_failed_check_writes_nothing(self):
        with mock.patch.object(share, 'check_history', return_value=['壞掉了']):
            ok, err = self.upd('2026-10-22')
        self.assertFalse(ok)
        self.assertIn('壞掉了', err)
        self.assertFalse(os.path.exists(os.path.join(self.hist, '20261022.json')))

    def test_file_date_must_match_data_date(self):
        res = res_for('2026-10-22')
        with self.assertRaises(ValueError):
            screener._snapshot(res, '20261021', self.data, self.hist, self.rules, share)

    def test_history_json_numbers_are_strings_and_rounded(self):
        rows = screener.share_rows(res_for('2026-10-22'))
        rows[:] = [r for r in rows if r['code'] == '1101'] + [r for r in rows if r['code'] != '1101']
        rows[0]['close'], rows[0]['chg'] = 120.123456789, -0.00001
        o = json.loads(screener.history_json(rows, '2026-10-22', 5, 5, {'1101': 7}))
        r = [x for x in o['rows'] if x[0] == '1101'][0]
        self.assertEqual((r[3], r[4], r[9]), ('120.1235', '-0.0', '8'))


class TestScanHistory(tu.TempCase):
    CODES = ('1101', '2330', '00646', '6874', '2454', '1216')

    def setUp(self):
        super().setUp()
        self.data = os.path.join(self.tmp, 'data')
        tu.make_data(self.data, self.CODES)
        self.work = os.path.join(self.tmp, 'work')
        os.makedirs(self.work)
        self.out = os.path.join(self.tmp, '結果')

    def scan(self, cfg_text, tweak=None):
        cfg = tu.write(os.path.join(self.tmp, '設定.txt'), cfg_text % dict(out=self.out))
        with mock.patch.object(screener, 'analyze', ts.fake_analyze_factory(tweak)), ts.Chdir(self.work):
            return tu.quiet(screener.cmd_scan, ts.ns(data=self.data, config=cfg))

    def test_pwa_off_leaves_no_history_and_a_plain_page(self):
        self.scan('share=1\noutput_dir=%(out)s\n')
        self.assertTrue(os.path.exists(os.path.join(self.out, '分享版_20261022.html')))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'cache')))
        raw = tu.readb(os.path.join(self.out, '分享版_20261022.html'))
        self.assertNotIn(b'manifest', raw)

    def test_pwa_on_writes_page_and_history(self):
        _, err = self.scan('share=1\nshare_pwa=1\noutput_dir=%(out)s\n')
        page = tu.readb(os.path.join(self.out, '分享版_20261022.html'))
        self.assertEqual(share.check_html(page, share.make_rules(capital=None, paths=[self.tmp, ROOT], sectors=screener.SECTOR_LABELS),
                                          pwa=True), [])
        h = os.path.join(self.tmp, 'cache', 'share', 'history')
        self.assertEqual(os.listdir(h), ['20261022.json'])
        o = json.loads(tu.readb(os.path.join(h, '20261022.json')))
        self.assertEqual((o['date'], o['n']), ('2026-10-22', 4))
        self.assertNotIn('歷史紀錄沒有寫入', err)
        _, err2 = self.scan('share=1\nshare_pwa=1\noutput_dir=%(out)s\n')
        self.assertEqual(os.listdir(h), ['20261022.json'])

    def test_bad_history_still_produces_the_page(self):
        with mock.patch.object(share, 'check_history', return_value=['壞']):
            _, err = self.scan('share=1\nshare_pwa=1\noutput_dir=%(out)s\n')
        self.assertIn('今天的歷史紀錄沒有寫入', err)
        self.assertTrue(os.path.exists(os.path.join(self.out, '分享版_20261022.html')))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'cache', 'share', 'history', '20261022.json')))

    def test_blocked_page_writes_no_history(self):
        def tweak(recs):
            recs['1101']['setup'] = '資金 1,000,000 元'
        self.scan('capital=1000000\nshare=1\nshare_pwa=1\noutput_dir=%(out)s\n', tweak)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'cache')))

    def test_config_key(self):
        cfg = screener.load_config(tu.write(os.path.join(self.tmp, 'a.txt'), 'share_pwa=1\n'))
        self.assertTrue(cfg['share_pwa'])
        self.assertFalse(screener.load_config(tu.write(os.path.join(self.tmp, 'b.txt'), 'share=1\n'))['share_pwa'])
        self.assertEqual(share.CONFIG_KEY_NAMES.count('share_pwa'), 1)
        self.assertFalse([l for l in screener.describe_config(screener.load_config(os.path.join(self.tmp, 'b.txt'))) if 'PWA' in l])
        self.assertTrue([l for l in screener.describe_config(cfg) if 'PWA' in l])
        self.assertTrue(tu.readb(os.path.join(ROOT, '設定.範例.txt')).decode('utf-8').count('share_pwa=0'))
        self.assertEqual(screener.share_history_dir(cfg), os.path.join(self.tmp, 'cache', 'share', 'history'))
        with self.assertRaises(screener.UserError):
            screener.load_config(tu.write(os.path.join(self.tmp, 'c.txt'), 'share_pwa=maybe\n'))


# ---------- 公開流程（Git Data API，gh 是假的）----------
class PwaCase(ts.GhCase):
    def setUp(self):
        super().setUp()
        self.hist = os.path.join(self.tmp, 'cache', 'share', 'history')
        self.day('20261022')

    def day(self, tag, page=True):
        date = '%s-%s-%s' % (tag[:4], tag[4:6], tag[6:])
        tu.write(os.path.join(self.hist, tag + '.json'), day_bytes(date))
        if page:
            self.write_day(tag, page=pwa_page(date))
        return date

    def pub(self, **k):
        return share.publish_pwa(REPO, 'full', self.out, self.hist, self.state, self.rules, say=self.said.append, **k)

    def first(self):
        self.assertEqual(self.pub(token=REPO), 0, self.said_text())

    def paths(self):
        return sorted(self.gh.files)

    def seed_remote(self, files):
        g = self.gh
        g.repo = dict(private=False, archived=False, default_branch='main')
        g.pages = True
        g.files = dict(files)
        g.commit_n = 1


class TestPwaConfirm(PwaCase):
    def test_unconfirmed_returns_10_with_zero_calls_and_lists_every_file(self):
        self.day('20261020', page=False)
        self.day('20261021', page=False)
        self.assertEqual(self.pub(), share.E_CONFIRM)
        self.assertEqual(self.gh.calls, [])
        self.assertFalse(os.path.exists(self.state))
        t = self.said_text()
        for s in ('【第一次公開 PWA 版前的確認】', REPO, 'https://yourname.github.io/tw-stock-screener-results/', 'index.html', 'sw.js',
                  'manifest.webmanifest', 'icons/icon.svg', 'icons/icon-192.png', 'icons/icon-512.png', 'icons/maskable-512.png',
                  'icons/apple-touch-icon.png', 'history/index.json', 'README.md', 'history/20261020.json', 'history/20261021.json',
                  'history/20261022.json', '3 個日檔，從 2026-10-20 到 2026-10-22',
                  '--confirm-pwa ' + REPO, '120', '無法收回', '離線資料', '候選標的 4 檔', '2026-10-22', '之後每天自動新增一天'):
            self.assertIn(s, t, s)
        self.assertNotIn('--confirm-first-publish', t)

    def test_state_bytes_untouched_when_unconfirmed(self):
        ts.share.save_state(self.state, dict(repo='someone/old', confirmed=True, last_blob='x'))
        before = tu.readb(self.state)
        self.assertEqual(self.pub(), 10)
        self.assertEqual(tu.readb(self.state), before)
        self.assertEqual(self.gh.calls, [])

    def test_wrong_token_is_15_with_zero_calls(self):
        for t in ('other/repo', '', 'yourname/tw-stock-screener-results-x'):
            self.assert_error(15, self.pub, token=t)
            self.assertEqual(self.gh.calls, [])

    def test_ask_without_tty_returns_10(self):
        self.answers = ['Y']
        self.assertEqual(self.pub(ask=True), 10)
        self.assertEqual((self.gh.calls, self.asked), ([], []))

    def test_tty_and_answers(self):
        for ans in ('N', '', 'no'):
            self.tty, self.answers = True, [ans]
            self.assertEqual(self.pub(ask=True), 10)
            self.assertEqual(self.gh.calls, [])
        self.tty, self.answers = True, ['Y']
        self.assertEqual(self.pub(ask=True), 0)
        self.assertTrue(self.gh.patches)

    def test_confirm_first_publish_state_is_not_enough(self):
        share.save_state(self.state, dict(repo=REPO, confirmed=True, last_blob='x', last_date='2026-10-01'))
        self.assertEqual(self.pub(), 10)                                      # 2 檔版確認過的，PWA 版還要再確認
        self.assertIn('只公開 index.html 和 README.md', self.said_text())
        self.assertEqual(self.gh.calls, [])

    def test_schema_mismatch_needs_confirmation_again(self):
        self.first()
        st = share.load_state(self.state)
        share.save_state(self.state, dict(st, pwa_schema=0))
        self.gh.calls.clear()
        self.assertEqual(self.pub(), 10)
        self.assertEqual(self.gh.calls, [])

    def test_stale_page_blocks(self):
        self.write_day('20261022', report=True)
        tu.write(os.path.join(self.out, '盤後選股_20261023.html'), 'x')
        self.assert_error(13, self.pub, token=REPO)

    def test_page_without_pwa_parts_is_13(self):
        self.write_day('20261022', page=ts.make_page('2026-10-22'))
        self.assert_error(13, self.pub, token=REPO)
        self.assertEqual(self.gh.calls, [])

    def test_bad_history_file_is_13_and_names_the_file(self):
        self.day('20261021', page=False)
        p = os.path.join(self.hist, '20261021.json')
        tu.write(p, tu.readb(p).replace(b'"120.0"', b'"1000000"'))
        e = self.assert_error(13, self.pub, token=REPO)
        self.assertIn('history/20261021.json', e.cause)
        self.assertEqual(self.gh.calls, [])

    def test_missing_today_history_is_13(self):
        os.remove(os.path.join(self.hist, '20261022.json'))
        e = self.assert_error(13, self.pub, token=REPO)
        self.assertIn('20261022', e.cause)

    def test_leak_in_page_is_13(self):
        p = os.path.join(self.out, '分享版_20261022.html')
        tu.write(p, tu.readb(p).decode('utf-8').replace('<main>', '<main><p>1,000,000</p>', 1))
        self.assert_error(13, self.pub, token=REPO)
        self.assertEqual(self.gh.calls, [])

    def test_list_mode_is_15(self):
        self.assert_error(15, share.publish_pwa, REPO, 'watchlist', self.out, self.hist, self.state, self.rules)


class TestPwaFlow(PwaCase):
    def test_first_publish_on_a_fresh_repo(self):
        self.day('20261020', page=False)
        self.first()
        g = self.gh
        self.assertEqual(len(g.named('repo', 'create')), 1)
        self.assertEqual(len(g.puts()), 1)                                    # 只有 README
        self.assertIn(b'manifest.webmanifest', g.pending_c and list(g.pending_c.values())[0]['README.md'])
        self.assertEqual((len(g.commit_bodies), len(g.patches), len(g.tree_bodies)), (1, 1, 1))
        self.assertEqual(len(g.named('-X', 'POST', 'repos/%s/pages' % REPO)), 1)
        self.assertEqual(self.paths(), sorted(['README.md', 'index.html', 'sw.js', 'manifest.webmanifest', 'history/index.json',
                                               'history/20261020.json', 'history/20261022.json', 'icons/icon.svg', 'icons/icon-192.png',
                                               'icons/icon-512.png', 'icons/maskable-512.png', 'icons/apple-touch-icon.png']))
        self.assertEqual(g.files['index.html'], tu.readb(os.path.join(self.out, '分享版_20261022.html')))
        self.assertEqual(json.loads(g.files['history/index.json'])['days'][0], dict(d='2026-10-22', n=4, b=1))
        self.assertEqual(g.patches[0]['force'], False)
        c = g.commit_bodies[0]
        self.assertEqual(c['author'], dict(name=LOGIN, email='4242+%s@users.noreply.github.com' % LOGIN))
        self.assertEqual(c['author'], c['committer'])
        self.assertEqual(len(c['parents']), 1)
        st = share.load_state(self.state)
        self.assertEqual((st['pwa_repo'], st['pwa_confirmed'], st['pwa_schema'], st['last_date'], st['confirmed']),
                         (REPO, True, 1, '2026-10-22', True))
        self.assertTrue(re.fullmatch(r'[0-9a-f]{40}', st['last_bundle']))
        self.assertIn('分享頁已經公開', self.said_text())

    def test_blob_order_and_index_html_is_last(self):
        self.day('20261020', page=False)
        self.first()
        posts = self.gh.blob_posts
        page = self.gh.files['index.html']
        self.assertEqual(posts[-1], page)
        self.assertTrue(posts[-2].startswith(b"var VER = '20261022-"))
        self.assertEqual(json.loads(posts[-3])['v'], 1)                          # history/index.json
        names = [next(p for p, b in self.gh.files.items() if b == x) for x in posts if x in self.gh.files.values()]
        self.assertEqual(names[0], 'history/20261020.json')
        self.assertEqual(names[1], 'history/20261022.json')
        self.assertEqual(names[-3:], ['history/index.json', 'sw.js', 'index.html'])
        self.assertLess(names.index('icons/icon.svg'), names.index('manifest.webmanifest'))

    def test_second_run_makes_zero_calls(self):
        self.first()
        n = len(self.gh.calls)
        self.said.clear()
        self.assertEqual(self.pub(), 0)
        self.assertEqual(len(self.gh.calls), n)
        self.assertIn('已經是最新', self.said_text())

    def test_older_data_makes_zero_calls(self):
        self.first()
        n = len(self.gh.calls)
        os.remove(os.path.join(self.out, '分享版_20261022.html'))
        os.remove(os.path.join(self.out, '盤後選股_20261022.html'))
        self.day('20261021')
        os.remove(os.path.join(self.hist, '20261022.json'))
        self.assertEqual(self.pub(), 0)
        self.assertEqual(len(self.gh.calls), n)

    def test_next_day_sends_exactly_four_blobs(self):
        self.first()
        g = self.gh
        n_blobs, n_patch = len(g.blob_posts), len(g.patches)
        self.day('20261023')
        self.said.clear()
        self.assertEqual(self.pub(), 0, self.said_text())
        new = g.blob_posts[n_blobs:]
        self.assertEqual(len(new), 4)
        self.assertEqual(len(g.patches), n_patch + 1)
        self.assertIn('history/20261023.json', g.files)
        self.assertEqual(json.loads(g.files['history/index.json'])['days'][0]['d'], '2026-10-23')
        self.assertEqual(len(g.puts()), 1)

    def test_nothing_changed_means_no_commit(self):
        self.first()
        st = share.load_state(self.state)
        share.save_state(self.state, dict(st, last_bundle='x'))               # 指紋對不上，但遠端其實已經一樣
        n = len(self.gh.commit_bodies)
        self.assertEqual(self.pub(), 0)
        self.assertEqual(len(self.gh.commit_bodies), n)
        self.assertNotEqual(share.load_state(self.state)['last_bundle'], 'x')

    def test_more_than_30_blobs_sleep_between(self):
        for i in range(1, 36):
            self.day((dt.date(2026, 9, 1) + dt.timedelta(days=i)).strftime('%Y%m%d'), page=False)
        self.first()
        self.assertGreaterEqual(len(self.gh.blob_posts), 31)
        self.assertEqual(self.sleeps.count(1), len(self.gh.blob_posts) - 1)

    def test_remote_with_125_days_deletes_only_the_oldest_ones_under_history(self):
        start = dt.date(2026, 5, 1)
        remote = {'README.md': share.readme_pwa('x')}
        days = [(start + dt.timedelta(days=i)).strftime('%Y%m%d') for i in range(125)]
        for d in days:
            remote['history/%s.json' % d] = day_bytes('%s-%s-%s' % (d[:4], d[4:6], d[6:]))
        self.seed_remote(remote)
        os.remove(os.path.join(self.hist, '20261022.json'))
        self.day('20261022')
        self.first()
        g = self.gh
        deleted = [e['path'] for e in g.tree_bodies[-1]['tree'] if e['sha'] is None]
        self.assertEqual(deleted, ['history/%s.json' % d for d in days[:6]])
        self.assertTrue(all(re.fullmatch(share.HIST_RE, p) for p in deleted))
        left = [p for p in g.files if re.fullmatch(share.HIST_RE, p)]
        self.assertEqual(len(left), 120)
        self.assertIn('history/20261022.json', left)
        self.assertEqual(len(os.listdir(self.hist)), 120)                      # 遠端獨有的日期還原到本機（只還原留得住的）
        self.assertTrue(os.path.exists(os.path.join(self.hist, days[-1] + '.json')))
        self.assertFalse(os.path.exists(os.path.join(self.hist, days[0] + '.json')))

    def test_invalid_remote_day_is_deleted_in_the_same_commit(self):
        remote = {'README.md': share.readme_pwa('x'), 'history/20261001.json': b'{"hacked":1}',
                  'history/20261002.json': day_bytes('2026-10-02')}
        self.seed_remote(remote)
        self.first()
        g = self.gh
        self.assertNotIn('history/20261001.json', g.files)
        self.assertIn('history/20261002.json', g.files)
        self.assertEqual(len(g.patches), 1)
        self.assertTrue(os.path.exists(os.path.join(self.hist, '20261002.json')))
        self.assertFalse(os.path.exists(os.path.join(self.hist, '20261001.json')))

    def test_unexpected_remote_files_stop_everything(self):
        for extra in ('secret.txt', 'history/x.txt', 'icons/evil.js', 'History/20261001.json', 'a.html'):
            self.gh = None
            self.setUp_gh()
            self.seed_remote({'README.md': b'r', extra: b'x'})
            e = self.assert_error(15, self.pub, token=REPO)
            self.assertIn('不是這個程式放的檔案', e.cause)
            g = self.gh
            self.assertEqual((g.blob_posts, g.tree_bodies, g.commit_bodies, g.patches, g.puts()), ([], [], [], [], []), extra)
            self.assertFalse(os.path.exists(self.state))

    def setUp_gh(self):
        self.gh = ts.FakeGh()
        p = mock.patch.object(share, '_run', self.gh)
        p.start()
        self.addCleanup(p.stop)

    def test_truncated_tree_is_15(self):
        self.seed_remote({'README.md': b'r'})
        self.gh.truncated = True
        self.assert_error(15, self.pub, token=REPO)
        self.assertEqual((self.gh.blob_posts, self.gh.patches), ([], []))

    def test_non_blob_or_wrong_mode_entries_are_15(self):
        self.seed_remote({'README.md': b'r'})
        real = self.gh.git

        def tweak(sub, method, kw):
            r = real(sub, method, kw)
            if sub.startswith('trees/') and method == 'GET':
                o = json.loads(r[1])
                o['tree'].append(dict(path='README.md.link', mode='120000', type='blob', sha='x'))
                return 0, json.dumps(o).encode(), b''
            return r
        self.gh.git = tweak
        self.assert_error(15, self.pub, token=REPO)

    def test_patch_422_is_14_and_state_is_not_saved(self):
        self.gh.script.append((lambda a: '-X' in a and a[a.index('-X') + 1] == 'PATCH', lambda a: self.gh.err(422, 'not fast forward')))
        self.assert_error(14, self.pub, token=REPO)
        self.assertFalse(os.path.exists(self.state))
        self.assertEqual(self.gh.patches, [])

    def test_patch_422_keeps_old_last_bundle(self):
        self.first()
        before = tu.readb(self.state)
        self.day('20261023')
        self.gh.script.append((lambda a: '-X' in a and a[a.index('-X') + 1] == 'PATCH', lambda a: self.gh.err(422, 'x')))
        self.assert_error(14, self.pub)
        self.assertEqual(tu.readb(self.state), before)

    def test_blob_sha_mismatch_is_14(self):
        self.gh.script.append((lambda a: any(x.endswith('/git/blobs') for x in a) and '-X' in a,
                               lambda a: self.gh.ok(dict(sha='0' * 40))))
        self.assert_error(14, self.pub, token=REPO)
        self.assertEqual(self.gh.patches, [])
        self.assertFalse(os.path.exists(self.state))

    def test_repo_missing_after_confirmed_is_15_and_resets(self):
        self.first()
        self.day('20261023')
        self.gh.repo = None
        self.assert_error(15, self.pub)
        st = share.load_state(self.state)
        self.assertEqual((st['confirmed'], st['pwa_confirmed']), (False, False))

    def test_pages_closed_after_confirmed_is_15_and_resets(self):
        self.first()
        self.day('20261023')
        self.gh.pages = False
        self.assert_error(15, self.pub)
        self.assertFalse(share.load_state(self.state)['pwa_confirmed'])

    def test_private_or_archived_repo_is_15(self):
        self.seed_remote({'README.md': b'r'})
        self.gh.repo['private'] = True
        self.assert_error(15, self.pub, token=REPO)
        self.gh.repo.update(private=False, archived=True)
        self.assert_error(15, self.pub, token=REPO)
        self.assertEqual(self.gh.patches, [])

    def test_wrong_login_is_15(self):
        self.gh.login = 'someone'
        self.assert_error(15, self.pub, token=REPO)

    def test_missing_gh_is_11_after_preview(self):
        with mock.patch.object(share, '_which', lambda n: None):
            self.assert_error(11, self.pub, token=REPO)
        self.assertIn('【第一次公開 PWA 版前的確認】', self.said_text())

    def test_two_file_state_fields_survive_a_two_file_publish(self):
        self.first()
        before = share.load_state(self.state)
        st = dict(before)
        share.save_state(self.state, st)
        self.assertEqual({k: v for k, v in share.load_state(self.state).items() if k.startswith('pwa') or k == 'last_bundle'},
                         {k: v for k, v in before.items() if k.startswith('pwa') or k == 'last_bundle'})

    def test_reset_confirmed_only_touches_pwa_field_when_present(self):
        share.save_state(self.state, dict(repo=REPO, confirmed=True))
        share._reset_confirmed(self.state, share.load_state(self.state))
        self.assertEqual(share.load_state(self.state), dict(repo=REPO, confirmed=False))


class TestPwaTakedown(PwaCase):
    def test_pwa_takedown_deletes_files_in_one_commit_then_closes_pages(self):
        self.day('20261020', page=False)
        self.first()
        g = self.gh
        n_commit, n_patch = len(g.commit_bodies), len(g.patches)
        self.tty, self.answers = True, [REPO, '']
        self.assertEqual(share.takedown(REPO, self.state, self.said.append), 0)
        self.assertEqual((len(g.commit_bodies), len(g.patches)), (n_commit + 1, n_patch + 1))
        self.assertEqual(sorted(g.files), ['README.md'])
        self.assertEqual(g.files['README.md'], share.README_GONE.encode('utf-8'))
        self.assertEqual(g.pages, False)
        st = share.load_state(self.state)
        self.assertEqual((st['confirmed'], st['pwa_confirmed']), (False, False))
        t = self.said_text()
        self.assertIn('一個 commit', t)
        self.assertIn('歷史紀錄裡還留著舊檔', t)

    def test_pwa_takedown_failure_still_closes_pages(self):
        self.first()
        self.gh.script.append((lambda a: '-X' in a and a[a.index('-X') + 1] == 'PATCH', lambda a: self.gh.err(422, 'x')))
        self.tty, self.answers = True, [REPO, '']
        self.assertEqual(share.takedown(REPO, self.state, self.said.append), 0)
        self.assertEqual(self.gh.pages, False)
        self.assertIn('沒辦法刪除網頁檔案', self.said_text())

    def test_non_pwa_takedown_is_unchanged(self):
        share.save_state(self.state, dict(repo=REPO, confirmed=True, last_blob='x'))
        self.gh.pages = True
        self.tty, self.answers = True, [REPO, '']
        self.assertEqual(share.takedown(REPO, self.state, self.said.append), 0)
        self.assertEqual(len(self.gh.calls), 1)
        self.assertNotIn('commit', self.said_text())
        self.assertEqual(share.load_state(self.state), dict(repo=REPO, confirmed=False, last_blob='x'))

    def test_non_interactive_pwa_takedown_says_what_would_happen_and_does_nothing(self):
        self.first()
        n = len(self.gh.calls)
        self.said.clear()
        self.assertEqual(share.takedown(REPO, self.state, self.said.append), 0)
        self.assertEqual(len(self.gh.calls), n)
        self.assertIn('PWA 版會先用一個 commit', self.said_text())


# ---------- screener 的 cmd_share 與參數 ----------
class TestCmdSharePwa(PwaCase):
    def setUp(self):
        super().setUp()
        self.cfgtext = 'share=1\nshare_publish=1\nshare_pwa=1\nshare_repo=%s\noutput_dir=%s\n' % (REPO, self.out)
        self.work = os.path.join(self.tmp, 'work')
        os.makedirs(self.work)
        p = mock.patch.object(screener, 'SHARE_STATE', self.state)
        p.start()
        self.addCleanup(p.stop)

    def call(self, text=None, **k):
        cfg = tu.write(os.path.join(self.tmp, '設定.txt'), self.cfgtext if text is None else text)
        a = dict(config=cfg, publish=True, ask=False, confirm_first_publish=None, confirm_pwa=None, takedown=None)
        a.update(k)
        with ts.Chdir(self.work):
            return tu.quiet(screener.cmd_share, argparse.Namespace(**a))

    def test_confirm_first_publish_with_pwa_on_is_15(self):
        with self.assertRaises(screener.UserError) as cm:
            self.call(publish=False, confirm_first_publish=REPO)
        self.assertEqual(cm.exception.code, 15)
        self.assertIn('--confirm-pwa', cm.exception.cause)
        self.assertEqual(self.gh.calls, [])

    def test_confirm_pwa_with_pwa_off_is_15(self):
        with self.assertRaises(screener.UserError) as cm:
            self.call(self.cfgtext.replace('share_pwa=1', 'share_pwa=0'), publish=False, confirm_pwa=REPO)
        self.assertEqual(cm.exception.code, 15)
        self.assertEqual(self.gh.calls, [])

    def test_confirm_pwa_with_publish_off_is_15(self):
        with self.assertRaises(screener.UserError) as cm:
            self.call(self.cfgtext.replace('share_publish=1', 'share_publish=0'), publish=False, confirm_pwa=REPO)
        self.assertEqual(cm.exception.code, 15)

    def test_bad_confirm_pwa_argument(self):
        with self.assertRaises(screener.UserError) as cm:
            self.call(publish=False, confirm_pwa='not a repo')
        self.assertEqual(cm.exception.code, 15)

    def test_unconfirmed_pwa_is_10_with_zero_calls(self):
        rc, err = self.call()
        self.assertEqual(rc, 10)
        self.assertIn('--confirm-pwa', err)
        self.assertEqual(self.gh.calls, [])
        self.assertFalse(os.path.exists(self.state))

    def test_confirm_pwa_publishes_using_history_next_to_the_config(self):
        rc, err = self.call(publish=False, confirm_pwa=REPO)
        self.assertEqual(rc, 0, err)
        self.assertTrue(share.load_state(self.state)['pwa_confirmed'])
        self.assertIn('history/20261022.json', self.gh.files)

    def test_pwa_off_uses_the_two_file_publish(self):
        self.write_day('20261022')                                          # 這個設定沒開 PWA，頁面就是 2 檔版的頁面
        with mock.patch.object(share, 'publish_pwa', side_effect=AssertionError) as m:
            rc, err = self.call(self.cfgtext.replace('share_pwa=1', 'share_pwa=0'))
        self.assertEqual(m.call_count, 0)
        self.assertEqual(rc, 10)
        self.assertIn('--confirm-first-publish', err)

    def test_pwa_off_publish_is_identical_to_today(self):
        """share_pwa 預設 0：公開出去的就是 2 個檔案（index.html、README.md），逐位元組和以前一樣"""
        self.write_day('20261022')
        page = tu.readb(os.path.join(self.out, '分享版_20261022.html'))
        rc, err = self.call('share=1\nshare_publish=1\nshare_repo=%s\noutput_dir=%s\n' % (REPO, self.out),
                            publish=False, confirm_first_publish=REPO)
        self.assertEqual(rc, 0, err)
        g = self.gh
        self.assertEqual(sorted(g.files), ['README.md', 'index.html'])
        self.assertEqual(g.files['index.html'], page)
        self.assertEqual(g.files['README.md'], share.README_TEMPLATE.format(url=share.pages_url('yourname', 'tw-stock-screener-results')).encode())
        self.assertEqual((g.blob_posts, g.tree_bodies, g.patches), ([], [], []))
        self.assertEqual(sorted(share.load_state(self.state)), sorted(share.STATE_FIELDS))

    def test_argparse_has_confirm_pwa(self):
        with mock.patch.object(sys, 'argv', ['screener.py', 'share', '--config', 'x.txt', '--confirm-pwa', REPO]), \
                mock.patch.object(screener, 'cmd_share', return_value=0) as m, self.assertRaises(SystemExit):
            screener.main()
        self.assertEqual(m.call_args[0][0].confirm_pwa, REPO)
        with mock.patch.object(sys, 'argv', ['screener.py', 'share', '--config', 'x.txt', '--confirm-pwa', REPO, '--publish']), \
                mock.patch('sys.stderr'), self.assertRaises(SystemExit) as cm:
            screener.main()
        self.assertEqual(cm.exception.code, 2)


# ---------- JS／CSS／SW 靜態檢查 ----------
class TestPwaStatic(unittest.TestCase):
    def texts(self):
        sw = share.sw_bytes('20261022-0123abcd', ['history/20261022.json', 'history/20261021.json']).decode('utf-8')
        return dict(PWA_JS=screener.PWA_JS, PWA_CSS=screener.PWA_CSS, SW=sw, SW_TEMPLATE=share.SW_TEMPLATE,
                    MANIFEST=share.manifest_bytes().decode('utf-8'), HEAD=share.PWA_HEAD_TAGS, HIST=screener.PWA_HIST,
                    BNAV=screener.PWA_BNAV)

    def test_zero_problems_with_leak_rules(self):
        r = share.make_rules(capital=1000000, paths=[ROOT], people=('someuser', 'SOMEPC'), repo='someowner/my-results')
        for name, t in self.texts().items():
            self.assertEqual(share._text_problems(t, r, [], [], []), [], name)
        r = share.make_rules(**RULES_KW)
        for name, t in self.texts().items():
            self.assertEqual(share._text_problems(t, r, [], [], []), [], name)

    def test_forbidden_constructs(self):
        for name in ('PWA_JS', 'PWA_CSS', 'SW', 'SW_TEMPLATE', 'MANIFEST'):
            t = re.sub(r'@@[A-Z]+@@', 'x', self.texts()[name])
            self.assertNotIn('//', t, name)
            self.assertIsNone(re.search(r'(?i)url\s*\(', t), name)
            self.assertNotIn('=>', t, name)
            self.assertNotIn('`', t, name)
            self.assertIsNone(re.search(r'\b(let|const)\s', t), name)
            self.assertIsNone(re.search(r'(?<![0-9A-Za-z_.])[0-9]{4,}(?![0-9A-Za-z_])', re.sub(r"""(["'])[^"'\n]*\1""", "''", t)), name)
            self.assertIsNone(re.search(r'[0-9],[0-9]{3}', t), name)
            self.assertIsNone(re.search(r'(?i)(?<![A-Za-z0-9_-])data\s*:', t), name)
            self.assertIsNone(re.search(r'(?i)\bmode\s*[=:]\s*[\'"=]|request\.mode', t), name)
            self.assertIsNone(re.search(r'https?://|[A-Za-z0-9._%+-]@', t), name)

    def test_required_pieces(self):
        js = screener.PWA_JS
        for s in ("'serviceWorker' in navigator", "register('sw.js')", 'window.fetch', "fetch('history/index.json'", 'no-cache',
                  'beforeinstallprompt', 'twss-a2hs', "textContent", 'getElementById'):
            self.assertIn(s, js, s)
        self.assertNotIn('createElementNS', js)
        self.assertNotIn('eval(', js)
        self.assertEqual(js.count('innerHTML'), 1)
        for line in js.splitlines():
            if 'localStorage' in line:
                self.assertIn('try', line)
        self.assertTrue(js.strip().startswith('(function () {'))
        self.assertTrue(js.strip().endswith('})();'))
        self.assertIn('catch (e) { }', js)

    def test_sw_has_no_absolute_paths_and_right_strategies(self):
        sw = self.texts()['SW']
        self.assertIsNone(re.search(r"""['"]/""", sw))
        for s in ("'twss-shell-'", "'twss-data'", "cache: 'reload'", 'skipWaiting', 'clients.claim', 'self.registration.scope',
                  "r.destination === 'document'", '404', '410', 'unregister', '3e3', "'history/index.json'", 'Promise.race',
                  "r.method !== 'GET'", 'twss-', 'Response.error()'):
            self.assertIn(s, sw, s)
        self.assertIn("var DAYS = [", sw)
        self.assertEqual(share.SW_TEMPLATE.count('@@'), 4)
        self.assertEqual(sorted(set(re.findall(r'@@[A-Z]+@@', share.SW_TEMPLATE))), ['@@DAYS@@', '@@VER@@'])

    def test_css_pieces(self):
        css = screener.PWA_CSS
        for s in ('env(safe-area-inset-bottom,0px)', 'min-height:44px', 'touch-action:manipulation', '@media (max-width:734px)',
                  'env(safe-area-inset-top,0px)', '@media (display-mode:standalone)', '.a2hs', 'padding-bottom'):
            self.assertIn(s, css, s)

    def test_glass_bar_css(self):
        css = screener.PWA_CSS
        self.assertNotIn('@@', css)
        mq = css[css.index('@media (max-width:734px){'):]
        mq = mq[:mq.index('\n}\n')]
        rule = mq[mq.index('.bnav{'):]
        rule = rule[:rule.index('}')]
        for s in ('position:fixed', 'border-radius:999px', '-webkit-backdrop-filter:blur(', 'backdrop-filter:blur(', 'saturate(',
                  'box-shadow:inset 0 1px 0', 'env(safe-area-inset-bottom,0px)', 'env(safe-area-inset-left,0px)',
                  'env(safe-area-inset-right,0px)', 'var(--glass)'):
            self.assertIn(s, rule, s)
        self.assertIn('.bnav{display:none}', css[:css.index('@media (max-width:734px)')])     # 桌機不顯示，和以前一樣
        # backdrop-filter 只用在 .bnav 這一個元素（不含關掉用的 none）
        for m in re.finditer(r'([^{}]*)\{[^{}]*backdrop-filter:(?!none)', css):
            self.assertEqual(m.group(1).strip(), '.bnav')
        for s in ('@supports not ((backdrop-filter:blur(1px)) or (-webkit-backdrop-filter:blur(1px))){.bnav{background:var(--nav);border-color:var(--ink3)}}',
                  '@media (prefers-reduced-transparency:reduce){.bnav{background:var(--nav);border-color:var(--ink3);',
                  '@media (forced-colors:active){.bnav{background:Canvas;border:1px solid CanvasText;'):
            self.assertIn(s, css, s)
        self.assertIn('.bnav a{position:relative;z-index:1;flex:1 1 0;min-height:44px', mq)
        for k in range(1, 3):
            self.assertIn('.bnav.at%d::before{transform:translateX(%d00%%)}' % (k, k), mq)
        self.assertEqual(screener.PWA_BNAV.count('<a href="#'), 3)                       # 鏡片寬度是 1/3
        self.assertIn('calc((100% - 8px) / 3)', mq)
        self.assertNotIn('outline:none', css)                                             # 焦點框沿用全域的 2px／3px
        self.assertEqual(emoji_hits(css), [])
        js = screener.PWA_JS
        for s in ("setAttribute('aria-current', 'location')", "removeAttribute('aria-current')", "'bnav on at' + k",
                  '{ passive: true }', 'requestAnimationFrame'):
            self.assertIn(s, js, s)
        self.assertLess(js.index("el('bnav')"), js.index("var bar = el('hbar')"))     # file:// 或沒有歷史時也會標出目前段落

    def test_only_motion_is_lens_slide(self):
        """整頁只有一個轉場：鏡片的 transform，≤ 180ms、ease-out，只在 prefers-reduced-motion:no-preference 裡；沒有 animation／@keyframes"""
        page = pwa_page()
        for t in (screener.PWA_CSS, page):
            self.assertNotRegex(t, r'animation|@keyframes')
            self.assertEqual(len(re.findall(r'transition', t)), 1)
        m = re.search(r'@media \(prefers-reduced-motion:no-preference\)\{\.bnav::before\{transition:transform ([0-9]+)ms ease-out\}\}',
                      screener.PWA_CSS)
        self.assertTrue(m)
        self.assertLessEqual(int(m.group(1)), 180)
        self.assertNotRegex(screener.HTML_CSS, r'transition|animation|@keyframes')

    def test_glass_contrast_worst_case(self):
        """玻璃列的字在最壞的底色上（頁面各種底色、漲跌色、純黑、純白；加上頂端反光、選取鏡片）仍 ≥ 4.5:1；焦點框 ≥ 3:1"""
        def over(top, base):
            a = top[3]
            return tuple(round(a * top[i] + (1 - a) * base[i]) for i in range(3))

        def hx(c):
            return '#%02x%02x%02x' % c

        def rgb(h):
            h = h.lstrip('#')
            return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
        backs = {'#000000', '#ffffff'}
        for p in screener.PALETTE.values():
            backs |= {p[k] for k in ('page', 'surface', 'nav', 'band', 'up', 'down', 'ink', 'ink2', 'link', 'warn', 'cap')}
        worst = {}
        for mode, g in screener.GLASS.items():
            p = screener.PALETTE[mode]
            self.assertGreaterEqual(g['glass'][3], .72 if mode == 'light' else .6, mode)
            low_text = low_lens = low_ring = 99
            for b in backs:
                for sheen in (0, g['sheen'][3]):                                 # 反光由上往下淡出：上緣最強、下半部沒有
                    c = over(g['glass'], rgb(b))
                    c = over(g['sheen'][:3] + (sheen,), c)
                    lens = over(g['lens'], c)
                    low_text = min(low_text, tu.wcag_ratio(p['ink'], hx(c)))
                    low_lens = min(low_lens, tu.wcag_ratio(p['ink'], hx(lens)))
                    low_ring = min(low_ring, tu.wcag_ratio(p['accent'], hx(c)))
            worst[mode] = (low_text, low_lens, low_ring)
            self.assertGreaterEqual(low_text, 4.5, mode)
            self.assertGreaterEqual(low_lens, 4.5, mode)
            self.assertGreaterEqual(low_ring, 3, mode)
            # 退回不透明（不支援模糊、減少透明度）時：nav 實色上的字
            self.assertGreaterEqual(tu.wcag_ratio(p['ink'], p['nav']), 7, mode)
            for k, v in g.items():
                self.assertIn('--%s:rgba(%d, %d, %d, %s)' % ((k.replace('_', '-'),) + v[:3] + (('%g' % v[3]).lstrip('0'),)),
                              screener.PWA_CSS, k)
        if os.environ.get('GLASS_PRINT'):
            print('\nglass worst-case (text, lens, ring):', {k: tuple(round(x, 2) for x in v) for k, v in worst.items()})

    def test_sizes(self):
        res = [tu.fake_rec('%d' % (1100 + i), '標的%d' % i, 50 + i, 0.5, 1 + i / 100, '回測型', 45 + i) for i in range(150)]
        big = screener.history_json(screener.share_rows(res), '2026-10-22', 2300, 2290, {r['code']: 12 for r in res})
        self.assertLess(len(big), 40_000)
        self.assertEqual(share.check_history(big, share.make_rules(capital=1000000, paths=[ROOT]), '20261022'), [])
        res = [tu.fake_rec('%d' % (1100 + i), '標的%d' % i, 50 + i, 0.5, 1 + i / 100, '回測型', 45 + i) for i in range(62)]
        mid = screener.history_json(screener.share_rows(res), '2026-10-22', 2300, 2290, {})
        self.assertTrue(4_000 < len(mid) < 15_000, len(mid))
        charts = {c: screener.chart_code(screener.chart_series(tu.rw_bars(150, i + 1)))
                  for i, c in enumerate(('1101', '2330', '00646', '6874'))}
        self.assertLess(len(pwa_page(charts=charts).encode('utf-8')), 300_000)
        for size, m in ((192, False), (512, False), (512, True), (180, False)):
            self.assertLess(len(share.png_icon(size, m)), 64_000)
        self.assertLess(len(share.sw_bytes('20261022-0123abcd', ['history/20261022.json'] * 5)), 16_000)
        self.assertLess(len(share.manifest_bytes()), 4_000)

    def test_run_bat_code_has_no_confirm(self):
        t = tu.readb(os.path.join(ROOT, 'run.bat')).decode('utf-8').split(tu.MARK.decode('ascii'))[0].lower()
        self.assertNotIn('confirm', t)
        self.assertNotIn('share_pwa', t)

    def test_pwa_module_stays_standalone(self):
        t = tu.readb(os.path.join(ROOT, 'share.py')).decode('utf-8')
        self.assertNotRegex(t, r'(?m)^\s*(import|from)\s+screener')
        self.assertNotIn('urllib', t)
        self.assertNotIn('shell=True', t)

    def test_example_config_documents_share_pwa(self):
        t = tu.readb(os.path.join(ROOT, '設定.範例.txt')).decode('utf-8')
        self.assertIn('\nshare_pwa=0', t)
        self.assertIn('--confirm-pwa', tu.readb(os.path.join(ROOT, 'README.md')).decode('utf-8'))


# 會被畫成 emoji 的字（Extended_Pictographic／Emoji=Yes 的常見範圍）；▲ ▼ ✓ ✗ 不是 emoji，可以用
EMOJI_RANGES = ((0x1F000, 0x1FAFF), (0x2600, 0x27BF), (0x2B50, 0x2B50), (0x2B55, 0x2B55), (0x25B6, 0x25B6), (0x25C0, 0x25C0),
                (0x25AA, 0x25AB), (0x25FB, 0x25FE), (0x23E9, 0x23FA), (0x2194, 0x2199), (0x21A9, 0x21AA), (0x231A, 0x231B),
                (0x2328, 0x2328), (0x23CF, 0x23CF), (0x2934, 0x2935), (0x2B05, 0x2B07), (0x2B1B, 0x2B1C), (0x3030, 0x3030),
                (0x303D, 0x303D), (0x3297, 0x3297), (0x3299, 0x3299), (0xA9, 0xA9), (0xAE, 0xAE), (0x203C, 0x203C),
                (0x2049, 0x2049), (0x2122, 0x2122), (0x2139, 0x2139), (0x24C2, 0x24C2), (0xFE0F, 0xFE0F))
EMOJI_OK = {0x2713, 0x2717}


def emoji_hits(t):
    return sorted({'U+%04X' % ord(ch) for ch in t if ord(ch) not in EMOJI_OK and any(a <= ord(ch) <= z for a, z in EMOJI_RANGES)})


class TestNoEmoji(tu.TempCase):
    def test_table_itself(self):
        self.assertEqual(emoji_hits('▲▼✓✗，。、：；「」（）／－–—…台股 K 線 MA20 −+'), [])
        for ch in '📈◀▶✔✅❌⚠★⭐©®™↔↩⏩':
            self.assertTrue(emoji_hits(ch), ch)

    def test_outputs_and_sources_have_no_emoji(self):
        res = tu.fake_results()
        charts = {c: screener.chart_code(screener.chart_series(tu.rw_bars(150, i + 1))) for i, c in enumerate(('1101', '2330', '2454'))}
        csvp = os.path.join(self.tmp, 'x.csv')
        screener.write_csv(csvp, res, 1000000)
        texts = dict(full=screener.render_html(res, False, tu.meta_for(charts=charts)), share=pwa_page(charts=charts),
                     plain=screener.render_share_html(res, None, charts), txt=screener.push_text(res, False),
                     md=screener.report(res, False), csv=tu.readb(csvp).decode('utf-8-sig'),
                     sw=share.sw_bytes('20261022-0123abcd', ['history/20261022.json']).decode('utf-8'),
                     manifest=share.manifest_bytes().decode('utf-8'), icon=share.ICON_SVG,
                     hist=screener.history_json(screener.share_rows(res), '2026-10-22', 2300, 2290, {}))
        for f in ('screener.py', 'share.py', 'fetch_data.py', 'README.md', 'CHANGELOG.md', '設定.範例.txt', 'run.bat', 'setup.bat',
                  'run_daily.bat', os.path.join('tests', 'test_usability.py'), os.path.join('tests', 'test_share.py')):
            texts[f] = tu.readb(os.path.join(ROOT, f)).decode('utf-8')
        for k, t in texts.items():
            if isinstance(t, bytes):
                t = t.decode('utf-8')
            self.assertEqual(emoji_hits(t), [], k)
        self.assertTrue(screener.push_text(res, False).startswith('盤後選股 '))


if __name__ == '__main__':
    unittest.main()
