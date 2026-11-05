"""易用性測試：設定檔、自選股清單、HTML／CSV 輸出、錯誤代碼、.bat 檔格式。只用 unittest，不連網。

執行：python -m unittest discover -s tests -v
"""
import argparse, contextlib, shutil, csv, datetime as dt, gzip, http.client, io, json, os, re, subprocess, sys, tempfile
import threading, time, unittest, _thread
import urllib.error, urllib.parse
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import fetch_data, screener  # noqa: E402


def write(path, data, enc='utf-8'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data if isinstance(data, bytes) else data.encode(enc))
    return path


def readb(path):
    with open(path, 'rb') as f:
        return f.read()


def quiet(fn, *a, **k):
    """執行 fn，回傳 (結果, stderr 文字)"""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        out = fn(*a, **k)
    return out, err.getvalue()


class TempCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix='測試 資料夾 (x86) ')
        self.tmp = self._td.name
        self.addCleanup(self._td.cleanup)


class TestThresholds(unittest.TestCase):
    def test_P_unchanged(self):
        self.assertEqual(screener.P, dict(
            wk_ma=(13, 26), vol_mult=1.3, vol_days=5, slope_days=5, cross_within=3, box_days=20, box_range=0.12,
            squeeze_lookback=120, squeeze_pct=0.25, long_red=0.03, ride_days=20, ride_ratio=0.8, pull_near=0.02,
            pull_vol=0.8, expand=1.2))


class TestConfig(TempCase):
    def cfg(self, text, enc='utf-8', name='設定.txt'):
        return write(os.path.join(self.tmp, name), text, enc)

    def test_basic_variants(self):
        text = ('\ufeff# 註解\r\n\r\n  capital = 1,000,000  \r\n;分號註解\r\nrisk = 3%  # c\r\n'
                'MODE=watchlist\r\noutput_dir="報告 輸出"\r\nwatchlist=my.csv\r\n')
        c, err = quiet(screener.load_config, self.cfg(text.encode('utf-8')))
        self.assertEqual(c['capital'], 1000000)
        self.assertEqual(c['risk'], 0.03)
        self.assertEqual(c['mode'], 'watchlist')
        self.assertEqual(c['output_dir'], os.path.join(self.tmp, '報告 輸出'))
        self.assertEqual(c['watchlist'], os.path.join(self.tmp, 'my.csv'))
        self.assertEqual(err, '')

    def test_defaults_and_fullwidth_equals(self):
        c, _ = quiet(screener.load_config, self.cfg('capital＝500000\n'))
        self.assertEqual((c['capital'], c['risk'], c['mode']), (500000, 0.02, 'full'))
        self.assertEqual(c['output_dir'], os.path.join(self.tmp, '結果'))

    def test_unknown_key_and_garbage_line_warn(self):
        c, err = quiet(screener.load_config, self.cfg('foo=1\nthis is junk\ncapital=100\n'))
        self.assertEqual(c['capital'], 100)
        self.assertIn('foo', err)
        self.assertIn('第 2 行看不懂', err)
        self.assertIn('capital', err)

    def test_duplicate_last_wins_and_empty_unset(self):
        c, _ = quiet(screener.load_config, self.cfg('capital=100\ncapital=200\nrisk=4%\nrisk=\n'))
        self.assertEqual(c['capital'], 200)
        self.assertEqual(c['risk'], 0.02)

    def test_cp950_ok_with_warning(self):
        c, err = quiet(screener.load_config, self.cfg('# 中文註解 台積電\ncapital=300000\n'.encode('cp950')))
        self.assertEqual(c['capital'], 300000)
        self.assertIn('Big5', err)

    def test_utf16_is_encoding_error(self):
        p = self.cfg('capital=1\n'.encode('utf-16'))
        with self.assertRaises(screener.UserError) as cm:
            quiet(screener.load_config, p)
        self.assertEqual(cm.exception.code, 8)
        self.assertIn('UTF-8', cm.exception.cause + cm.exception.todo)

    def test_bad_risk_reports_line(self):
        p = self.cfg('capital=1\nrisk=8%\n')
        with self.assertRaises(screener.UserError) as cm:
            screener.load_config(p)
        self.assertEqual(cm.exception.code, 9)
        self.assertIn('第 2 行', cm.exception.cause)
        self.assertIn('設定.範例.txt', cm.exception.todo)

    def test_missing_config(self):
        with self.assertRaises(screener.UserError) as cm:
            screener.load_config(os.path.join(self.tmp, 'nope.txt'))
        self.assertEqual(cm.exception.code, 9)

    def test_parse_capital(self):
        pc = screener.parse_capital
        self.assertEqual(pc('1,000,000'), 1000000)
        self.assertEqual(pc(' 1000000 '), 1000000)
        self.assertIsNone(pc(''))
        self.assertIsNone(pc('0'))
        for bad in ('abc', '"', '100萬', '-5'):
            with self.assertRaises(ValueError, msg=bad):
                pc(bad)

    def test_parse_risk(self):
        pr = screener.parse_risk
        for v, want in (('', 0.02), ('2', 0.02), ('2%', 0.02), ('0.03', 0.03), ('2.5％', 0.025), ('5', 0.05)):
            self.assertEqual(pr(v), want, v)
        for bad in ('1', '8', '1%', 'abc', '!PATH!'):
            with self.assertRaises(ValueError, msg=bad) as cm:
                pr(bad)
            self.assertNotIn('unsupported', str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            pr('8')
        self.assertIn('2% 到 5%', str(cm.exception))

    def test_parse_mode(self):
        pm = screener.parse_mode
        for v in ('', 'full', 'ALL', '1', '全市場'):
            self.assertEqual(pm(v), 'full')
        for v in ('watchlist', 'list', '2', '自選股', '自選'):
            self.assertEqual(pm(v), 'watchlist')
        with self.assertRaises(ValueError):
            pm('3')

    def test_write_config_preserves_other_lines(self):
        p = self.cfg('# 我的設定\r\ncapital=1\r\nrisk=2%\r\nmode=full\r\noutput_dir=D:\\選股結果\r\n')
        screener.write_config(p, 2500000, 0.03, 'watchlist')
        raw = readb(p)
        self.assertFalse(raw.startswith(b'\xef\xbb\xbf'))
        self.assertEqual(raw.count(b'\r\n'), raw.count(b'\n'))
        text = raw.decode('utf-8')
        self.assertIn('# 我的設定', text)
        self.assertIn('output_dir=D:\\選股結果', text)
        c, _ = quiet(screener.load_config, p)
        self.assertEqual((c['capital'], c['risk'], c['mode']), (2500000, 0.03, 'watchlist'))

    def test_write_config_from_template_when_missing(self):
        p = os.path.join(self.tmp, '設定.txt')
        screener.write_config(p, None, 0.02, 'full')
        c, _ = quiet(screener.load_config, p)
        self.assertEqual((c['capital'], c['risk'], c['mode']), (None, 0.02, 'full'))
        self.assertIn('#', readb(p).decode('utf-8'))


class TestWatchlist(TempCase):
    def lst(self, data, enc='utf-8', name='自選股.csv'):
        return write(os.path.join(self.tmp, name), data, enc)

    def code_of(self, fn):
        with self.assertRaises(screener.UserError) as cm:
            quiet(fn)
        return cm.exception

    def test_normal_and_bom(self):
        p = self.lst('\ufeff代號,名稱,備註\r\n2330,台積電,\r\n0050,元大台灣50,x\r\n')
        wl, _ = quiet(screener.load_list, p)
        self.assertEqual(wl, {'2330': '台積電', '0050': '元大台灣50'})

    def test_cp950(self):
        p = self.lst('代號,名稱\n2330,台積電\n'.encode('cp950'))
        wl, err = quiet(screener.load_list, p)
        self.assertEqual(wl, {'2330': '台積電'})
        self.assertIn('Big5', err)

    def test_excel_text_formula(self):
        p = self.lst('代號,名稱\n="0050",元大\n"=""0056""",高股息\n')
        wl, _ = quiet(screener.load_list, p)
        self.assertEqual(sorted(wl), ['0050', '0056'])

    def test_none_path(self):
        self.assertIsNone(screener.load_list(None))

    def test_bad_header(self):
        e = self.code_of(lambda: screener.load_list(self.lst('code,name\n2330,x\n')))
        self.assertEqual(e.code, 7)
        self.assertIn('代號', e.cause)

    def test_empty_list(self):
        e = self.code_of(lambda: screener.load_list(self.lst('代號,名稱\n')))
        self.assertEqual(e.code, 7)

    def test_missing(self):
        e = self.code_of(lambda: screener.load_list(os.path.join(self.tmp, '自選股.csv')))
        self.assertEqual(e.code, 7)
        self.assertIn('自選股.範例.csv', e.todo)

    def test_missing_but_dot_csv_dot_txt_exists(self):
        self.lst('代號\n2330\n', name='自選股.csv.txt')
        e = self.code_of(lambda: screener.load_list(os.path.join(self.tmp, '自選股.csv')))
        self.assertEqual(e.code, 7)
        self.assertIn('自選股.csv.txt', e.todo)

    def test_utf16(self):
        e = self.code_of(lambda: screener.load_list(self.lst('代號,名稱\n2330,x\n'.encode('utf-16'))))
        self.assertEqual(e.code, 8)


def fake_rec(code, name, close, chg, vr, typ=None, stop=None, watch=False, stages=None, date='2026-10-22'):
    st = {0: True, 1: True, 2: bool(watch or typ), 3: bool(watch or typ), 'A': typ == '突破型', 'B': typ == '回測型'}
    st.update(stages or {})
    r = dict(code=code, name=name, date=date, close=close, chg=chg, stages=st, notes=[], vol_ratio=vr,
             osc=[-0.5, 0.7], watch=watch)
    if typ:
        r.update(type=typ, stop=stop, setup='近 20 日沿月線 & <測試>', stop_why='跌破 MA20 <b>x</b>')
    return r


def fake_results(capital=1000000, risk=0.02):
    res = [fake_rec('1101', '台泥', 120.0, 3.1, 2.4, '突破型', 117.0),
           fake_rec('2330', '台積電', 2385.0, -0.8, 0.6, '回測型', 2300.0),
           fake_rec('00646', '元大S&P500', 78.95, -0.32, 0.6, '回測型', 77.76),
           fake_rec('6874', '寬停損', 100.0, 0.0, 0.7, '回測型', 85.0),
           fake_rec('2454', '聯發科', 1200.0, 1.5, 1.8, watch=True),
           fake_rec('1216', '統一', 70.0, 0.3, 1.0, stages={0: False}),
           dict(code='9999', name='新股', error='資料只有 10 天，至少要 80 天')]
    for r in res:
        screener.size(r, capital, risk)
    return res


def meta_for(**k):
    m = dict(capital=1000000, risk=0.02, list_path=None, list_count=0, missing=[], today='2026-10-22',
             generated='2026-10-22 15:00')
    m.update(k)
    return m


def wcag_ratio(a, b):
    """WCAG 2.x 對比值（#rrggbb）"""
    def lum(h):
        h = h.lstrip('#')
        c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        c = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    x, y = sorted((lum(a), lum(b)), reverse=True)
    return (x + 0.05) / (y + 0.05)


TEXT_TOKENS = ('ink', 'ink2', 'link', 'up', 'down', 'warn', 'cap')     # 會拿來寫字的顏色
BACKGROUNDS = ('page', 'surface', 'nav')


class TestRender(unittest.TestCase):
    def test_html_content(self):
        res = fake_results()
        h = screener.render_html(res, False, meta_for())
        self.assertIn('lang="zh-Hant"', h)
        self.assertIn('charset="utf-8"', h)
        self.assertIn('元大S&amp;P500', h)
        self.assertNotIn('<測試>', h)
        self.assertNotIn('<b>x</b>', h)
        for cls in ('sortable', 't-break', 't-pull', 'p-cap', 'p-odd', 'd-wide', 'd-ok'):
            self.assertIn(cls, h)
        self.assertIn('2026-10-22（星期四）', h)
        self.assertIn('掃描 7 檔（資料有效 6 檔）', h)
        self.assertIn('部位（風險 2%）', h)
        self.assertIn('部位（5%）', h)
        self.assertIn('已達總資金上限', h)
        self.assertIn('不到 1 張，零股', h)
        self.assertIn('觀察名單', h)
        self.assertNotIn('資料日期不是今天', h)
        self.assertIn('資料只有 10 天', h)
        self.assertIn('僅供研究，非投資建議', h)
        self.assertIn('怎麼看這一頁', h)

    def test_no_external_resources(self):
        h = screener.render_html(fake_results(), True, meta_for())
        for bad in ('http://', 'https://', '<link', 'src=', '@import', 'url(', '<iframe', '<img', '//cdn'):
            self.assertNotIn(bad, h)
        self.assertEqual(h.count('<script'), 3)            # 主題（head）+ 明細資料 + 頁面程式，都內嵌

    def test_both_color_schemes_from_palette(self):
        h = screener.render_html(fake_results(), False, meta_for())
        self.assertIn('@media (prefers-color-scheme:dark)', h)
        self.assertIn(':root[data-theme="dark"]', h)
        for mode in ('light', 'dark'):
            self.assertIn(screener._css_vars(screener.PALETTE[mode]), h)
        self.assertEqual(set(screener.PALETTE['light']), set(screener.PALETTE['dark']))

    def test_contrast(self):
        """主要、次要文字 ≥ 7:1（AAA）；其他文字 ≥ 4.5:1；按鈕文字 ≥ 4.5:1；有意義的框線（ink3）≥ 4.5:1；
        圖形記號、焦點框 ≥ 3:1（淺色、深色都要）"""
        for mode, p in screener.PALETTE.items():
            for fg in TEXT_TOKENS:
                for bg in BACKGROUNDS:
                    need = 7 if fg in ('ink', 'ink2') else 4.5
                    self.assertGreaterEqual(wcag_ratio(p[fg], p[bg]), need, f'{mode} {fg} on {bg}')
                self.assertGreaterEqual(wcag_ratio(p['ink3'], p[bg]), 4.5, f'{mode} 框線 ink3 on {bg}')
            for fg in ('up', 'down', 'cap'):
                for bg in ('page', 'surface'):      # 數字的漲跌色也到 7:1
                    self.assertGreaterEqual(wcag_ratio(p[fg], p[bg]), 7, f'{mode} {fg} on {bg}')
            # 價位條：布林帶要看得出來，停損價（warn）與收盤點（ink）畫在帶上也要看得清楚
            self.assertGreaterEqual(wcag_ratio(p['band'], p['surface']), 1.7, f'{mode} band')
            self.assertGreaterEqual(wcag_ratio(p['warn'], p['band']), 3.5, f'{mode} warn on band')
            self.assertGreaterEqual(wcag_ratio(p['ink'], p['band']), 4.5, f'{mode} ink on band')
            for bg in ('btn', 'btn_hover', 'btn_active'):
                self.assertGreaterEqual(wcag_ratio(p['on_btn'], p[bg]), 4.5, f'{mode} on_btn on {bg}')
            self.assertGreaterEqual(wcag_ratio(p['page'], p['ink']), 4.5, f'{mode} 選取中的篩選按鈕')
            for fg in ('accent', 'btn', 'ink', 'ink2', 'ink3', 'warn'):
                for bg in ('page', 'surface'):
                    self.assertGreaterEqual(wcag_ratio(p[fg], p[bg]), 3, f'{mode} mark {fg} on {bg}')
        # CSS 裡用來寫字的顏色，只能是上面檢查過的
        used = set(re.findall(r'[;{]color:var\(--([a-z0-9-]+)\)', screener.HTML_CSS))
        self.assertTrue(used)
        self.assertLessEqual(used, set(TEXT_TOKENS) | {'on-btn', 'page'})
        self.assertAlmostEqual(wcag_ratio('#ffffff', '#000000'), 21.0)
        self.assertAlmostEqual(wcag_ratio('#777777', '#ffffff'), 4.48, places=2)

    def test_up_down_not_color_only(self):
        h = screener.render_html(fake_results(), False, meta_for())
        self.assertIn('<span class="up">▲ +3.10%</span>', h)
        self.assertIn('<span class="down">▼ -0.80%</span>', h)
        self.assertIn('<span class="flat">平 0.00%</span>', h)
        for word in ('<span class="g">近</span>', '<span class="g">遠</span>'):
            self.assertIn(word, h)
        self.assertIn('>突破型</span>', h)
        self.assertIn('>回測型</span>', h)

    def test_filter_sort_controls(self):
        h = screener.render_html(fake_results(), False, meta_for())
        for s in ('name="ft"', 'name="fk"', 'id="cq"', 'id="csort"', 'aria-live="polite"', "'aria-sort'",
                  '全部 4', '突破型 1', '回測型 3', '個股 3', 'ETF 1', 'data-kind="etf" data-code="00646"'):
            self.assertIn(s, h)
        self.assertEqual(h.count('<tbody class="cand"'), 4)
        self.assertGreaterEqual(h.count('<button type="button" class="sb"'), 8 + 7 + 11)

    def test_price_bar(self):
        res = fake_results()
        res[0]['ma'] = dict(ma5=121, ma20=116.5, ma60=110, boll_up=125.2, boll_lo=104.8)
        h = screener.render_html(res, False, meta_for())
        self.assertIn('停損 117.0</span><span class="k k-ma">MA20 116.5</span>', h)
        self.assertIn('收盤 120.0', h)
        self.assertIn('布林 104.8～125.2', h)
        self.assertEqual(h.count('class="pb" aria-hidden="true"'), 4)    # 沒有 ma 的也畫得出來

    def test_lazy_details(self):
        res = fake_results()
        res.append(fake_rec('1234', '</script><b>&', 10.0, 1.0, 1.0))
        h = screener.render_html(res, False, meta_for())
        self.assertIn('<details id="stages-det">', h)
        self.assertIn('<noscript>', h)
        self.assertIn('<tbody id="st-body"></tbody>', h)
        self.assertEqual(h.count('</script>'), 3)
        m = re.search(r'<script type="application/json" id="stage-data">(.*?)</script>', h)
        pack = json.loads(m.group(1))
        rows = pack['r']
        self.assertEqual(pack['s'], [''])
        self.assertEqual(len(rows), 7)
        self.assertEqual(rows[0], ['1101', '台泥', '120.0', 0b011111, 2.4, '-0.5', '0.7', 0])
        self.assertEqual(rows[5][3], 0b000010)          # 1216：只過第 1 關
        self.assertEqual(rows[6][1], '</script><b>&')
        self.assertNotIn('1216', h.replace(m.group(0), ''))   # 明細列只在 JSON 裡，展開才產生

    def test_size_budget_large_input(self):
        res = []
        for i in range(2400):
            typ = ('突破型', '回測型')[i % 2] if i % 80 == 0 else None
            r = fake_rec('%04d' % (1000 + i), '測試股票名稱%d' % i, 100.5 + i, 1.23, 1.5, typ,
                         95.0 + i if typ else None, watch=(i % 70 == 1))
            r['ma'] = dict(ma5=101, ma20=99.1 + i, ma60=98, boll_up=110.3 + i, boll_lo=90.2 + i)
            r['notes'] = ['月線與季線差距沒有擴大（整理中）'] if i % 10 == 0 else []
            screener.size(r, 1000000, 0.02)
            res.append(r)
        h = screener.render_html(res, False, meta_for())
        self.assertLess(len(h.encode('utf-8')), 500_000)

    def test_list_mode_titles_and_missing(self):
        res = fake_results()
        h = screener.render_html(res, True, meta_for(list_path='C:\\x\\自選股.csv', list_count=9, missing=['50', '9999']))
        self.assertIn('自選股：過第 2、3 關但型態未到', h)
        self.assertIn('自選股清單（自選股.csv，9 檔）', h)
        self.assertIn('找不到資料：50、9999', h)
        self.assertNotIn('C:\\x', h)

    def test_no_capital_and_stale_date_and_empty(self):
        res = [fake_rec('1216', '統一', 70.0, 0.3, 1.0, stages={0: False})]
        h = screener.render_html(res, False, meta_for(capital=None, today='2026-10-25'))
        self.assertIn('未設定總資金，不試算部位', h)
        self.assertNotIn('部位（', h)
        self.assertIn('資料日期不是今天', h)
        self.assertIn('今天沒有標的同時通過所有關卡。', h)

    def test_risk_5_has_single_position_column(self):
        h = screener.render_html(fake_results(risk=0.05), False, meta_for(risk=0.05))
        self.assertIn('部位（風險 5%）', h)
        self.assertNotIn('部位（5%）', h)

    def test_render_does_not_mutate(self):
        res = fake_results()
        res[0]['ma'] = dict(ma5=121, ma20=116.5, ma60=110, boll_up=125.2, boll_lo=104.8)
        before = json.dumps(res, ensure_ascii=False, default=str)
        screener.render_html(res, True, meta_for())
        screener.render_html(res, False, meta_for(capital=None))
        with tempfile.TemporaryDirectory() as d:
            screener.write_csv(os.path.join(d, 'a.csv'), res, 1000000)
        self.assertEqual(before, json.dumps(res, ensure_ascii=False, default=str))


class TestCsv(TempCase):
    def test_csv(self):
        p = os.path.join(self.tmp, 'o.csv')
        res = fake_results()
        res.append(fake_rec('0050', '元大台灣50', 150.0, 0.1, 1.0))
        screener.write_csv(p, res, 1000000)
        raw = readb(p)
        self.assertTrue(raw.startswith(b'\xef\xbb\xbf'))
        rows = list(csv.reader(io.StringIO(raw.decode('utf-8-sig'), newline='')))
        self.assertEqual(rows[0], screener.CSV_HEAD)
        self.assertEqual(rows[0][:5], ['代號', '名稱', '資料日期', '型態', '收盤'])
        self.assertEqual(rows[0][-5:], ['說明', '停損依據', '備註', '類股', '市場'])
        self.assertTrue(all(len(r) == len(rows[0]) for r in rows))
        codes = [r[0] for r in rows[1:]]
        self.assertIn('="0050"', codes)
        self.assertEqual(codes[-1], '="9999"')              # 錯誤列排最後
        self.assertIn('資料只有 10 天', rows[-1][-3])
        self.assertEqual(rows[-1][-2:], ['', ''])
        self.assertEqual([bool(r[3]) for r in rows[1:]], [True] * 4 + [False] * 4)     # 候選在前，其餘在後，錯誤列最後
        first = rows[1]
        self.assertIn(first[13], ('是', '否'))
        self.assertEqual(sorted({c for r in rows[1:-1] for c in r[13:19]}), ['否', '是'])


def make_data(d, codes=('1101', '2330'), n=150):
    os.makedirs(d, exist_ok=True)
    start = dt.date(2026, 3, 2)
    days = []
    while len(days) < n:
        if start.weekday() < 5:
            days.append(start.isoformat())
        start += dt.timedelta(days=1)
    for k, code in enumerate(codes):
        with open(os.path.join(d, code + '.csv'), 'w', encoding='utf-8', newline='') as f:
            w = csv.writer(f)
            w.writerow(['date', 'open', 'high', 'low', 'close', 'volume', 'raw_close', 'name'])
            for i, day in enumerate(days):
                c = round(100 + k * 50 + i * 0.3 + (i % 7) * 0.4, 2)
                w.writerow([day, c - 0.2, c + 0.8, c - 0.9, c, 1000000 + (i % 5) * 50000, c, '測試' + code])
    return days[-1].replace('-', '')


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith('TWSTOCK_') and k not in ('PYTHONUTF8', 'PYTHONIOENCODING')}
    env.update(extra)
    return env


class TestScanCli(TempCase):
    def run_cli(self, *args, **envx):
        env = clean_env(PYTHONUTF8='1')
        env.update(envx)
        return subprocess.run([sys.executable, os.path.join(ROOT, 'screener.py')] + list(args), cwd=self.tmp, env=env,
                              capture_output=True, timeout=120)

    def test_out_dir_writes_five_files(self):
        d = os.path.join(self.tmp, 'data')
        tag = make_data(d)
        out = os.path.join(self.tmp, '結果 輸出')
        r = self.run_cli('scan', '--data', d, '--out-dir', out, '--capital', '1000000')
        self.assertEqual(r.returncode, 0, r.stderr.decode('utf-8', 'replace'))
        self.assertEqual(sorted(os.listdir(out)), sorted(f'盤後選股_{tag}.{e}' for e in ('html', 'csv', 'md', 'txt', 'json')))
        self.assertIn('選股完成', r.stderr.decode('utf-8'))

    def test_missing_data(self):
        r = self.run_cli('scan', '--data', os.path.join(self.tmp, 'nope'))
        self.assertEqual(r.returncode, 6)
        self.assertIn('原因：', r.stderr.decode('utf-8'))

    def test_bad_config(self):
        d = os.path.join(self.tmp, 'data')
        make_data(d)
        cfg = write(os.path.join(self.tmp, '設定.txt'), 'risk=8%\n')
        r = self.run_cli('scan', '--data', d, '--config', cfg)
        self.assertEqual(r.returncode, 9)
        e = r.stderr.decode('utf-8')
        self.assertIn('第 1 行', e)

    def test_bad_watchlist_header(self):
        d = os.path.join(self.tmp, 'data')
        make_data(d)
        lst = write(os.path.join(self.tmp, 'l.csv'), 'code,name\n1101,x\n')
        r = self.run_cli('scan', '--data', d, '--list', lst)
        self.assertEqual(r.returncode, 7)
        self.assertIn('代號', r.stderr.decode('utf-8'))

    def test_watchlist_nothing_found(self):
        d = os.path.join(self.tmp, 'data')
        make_data(d)
        lst = write(os.path.join(self.tmp, 'l.csv'), '代號,名稱\n50,x\n')
        r = self.run_cli('scan', '--data', d, '--list', lst)
        self.assertEqual(r.returncode, 7)
        self.assertIn('50', r.stderr.decode('utf-8'))

    def test_watchlist_partial_notice(self):
        d = os.path.join(self.tmp, 'data')
        make_data(d)
        lst = write(os.path.join(self.tmp, 'l.csv'), '代號,名稱\n1101,x\n50,y\n')
        r = self.run_cli('scan', '--data', d, '--list', lst, '--out', os.path.join(self.tmp, 'o.md'))
        self.assertEqual(r.returncode, 0)
        self.assertIn('找不到資料：50', r.stderr.decode('utf-8'))

    def test_ascii_console_without_out(self):
        d = os.path.join(self.tmp, 'data')
        make_data(d)
        env = clean_env(PYTHONIOENCODING='ascii')
        r = subprocess.run([sys.executable, os.path.join(ROOT, 'screener.py'), 'scan', '--data', d], cwd=self.tmp,
                           env=env, capture_output=True, timeout=120)
        self.assertEqual(r.returncode, 8)
        self.assertNotIn('✓'.encode('utf-8'), r.stderr)

    def test_resolve_scan_options(self):
        cfgp = write(os.path.join(self.tmp, '設定.txt'), 'capital=500000\nrisk=3%\nmode=watchlist\nwatchlist=w.csv\n')
        ns = lambda **k: argparse.Namespace(**dict(dict(config=None, capital=None, risk=None, list=None, out_dir=None), **k))
        o = screener.resolve_scan_options(ns())
        self.assertEqual((o['capital'], o['risk'], o['list'], o['out_dir']), (None, 0.02, None, None))
        o = screener.resolve_scan_options(ns(config=cfgp))
        self.assertEqual((o['capital'], o['risk']), (500000, 0.03))
        self.assertEqual(o['list'], os.path.join(self.tmp, 'w.csv'))
        self.assertEqual(o['out_dir'], os.path.join(self.tmp, '結果'))
        o = screener.resolve_scan_options(ns(config=cfgp, capital=1.0, risk=0.04, list='x.csv', out_dir='o'))
        self.assertEqual((o['capital'], o['risk'], o['list'], o['out_dir']), (1.0, 0.04, 'x.csv', 'o'))


def isolate_net(tc, sleep_fails=True, **over):
    """測試用：快取／資料指到暫存資料夾、不連網、不真的等待、不印 log"""
    vals = dict(GAP=0, RETRY=(), CACHE=os.path.join(tc.tmp, 'cache'), DATA=os.path.join(tc.tmp, 'data'))
    vals.update(over)
    patches = [mock.patch.object(fetch_data, k, v) for k, v in vals.items()]
    patches += [mock.patch.object(fetch_data, '_proxied', return_value=False),
                mock.patch.object(fetch_data.http.client, 'HTTPSConnection', side_effect=AssertionError('不該連網')),
                mock.patch.object(fetch_data.urllib.request, 'urlopen', side_effect=AssertionError('不該連網')),
                mock.patch.object(fetch_data, 'log')]
    if sleep_fails:
        patches.append(mock.patch.object(fetch_data, '_sleep', side_effect=AssertionError('不該等待')))
    fetch_data._reset_net()
    tc.addCleanup(fetch_data._reset_net)
    for p in patches:
        p.start()
        tc.addCleanup(p.stop)


class TestFetch(TempCase):
    def setUp(self):
        super().setUp()
        isolate_net(self)

    ROW = ['1101', '台泥', 10.0, 11.0, 9.0, 10.5, 1000]

    def preload(self):
        """快取放一個舊日期；今天是平日的話連今天也放，避免觸發 600 秒等待"""
        today = dt.date.today()
        old = today - dt.timedelta(days=5)
        while old.weekday() >= 5:
            old -= dt.timedelta(days=1)
        days = [old] + ([today] if today.weekday() < 5 else [])
        for d in days:
            for m in ('twse', 'tpex'):
                fetch_data.write_day(m, d.isoformat(), [self.ROW])

    def test_http_json_kinds(self):
        def kind(resp):
            with mock.patch.object(fetch_data, '_open', side_effect=resp if isinstance(resp, Exception) else None,
                                   return_value=None if isinstance(resp, Exception) else resp):
                with self.assertRaises(fetch_data.FetchError) as cm:
                    fetch_data.http_json('https://example.invalid/')
            return cm.exception
        e = kind(OSError('x'))
        self.assertEqual((e.kind, e.blocked), ('network', False))
        for code in (403, 429):
            e = kind((code, {}, b''))
            self.assertEqual((e.kind, e.blocked), ('site', True))
        e = kind((200, {}, b'  <html><body>blocked</body></html>'))
        self.assertEqual((e.kind, e.blocked), ('site', True))
        self.assertIn('網頁', str(e))
        for resp in ((500, {}, b''), (302, {'Location': 'https://x/errors'}, b''), (200, {}, b'oops'),
                     (200, {'Content-Encoding': 'gzip'}, b'not gzip'), (200, {'Content-Encoding': 'br'}, b'{}')):
            e = kind(resp)
            self.assertEqual((e.kind, e.blocked), ('site', False), resp)
        ok = gzip.compress('{"stat":"OK","名稱":"台泥"}'.encode('utf-8'))
        with mock.patch.object(fetch_data, '_open', return_value=(200, {'Content-Encoding': 'gzip'}, ok)):
            self.assertEqual(fetch_data.http_json('https://example.invalid/')['名稱'], '台泥')
        with mock.patch.object(fetch_data, '_open', return_value=(200, {}, '{"a":1}'.encode('utf-8-sig'))):
            self.assertEqual(fetch_data.http_json('https://example.invalid/'), {'a': 1})

    def test_http_json_via_proxy_path(self):
        class R:
            status = 200
            headers = {}

            def __init__(self, body, url):
                self.body, self.url = body, url

            def read(self):
                return self.body

            def geturl(self):
                return self.url

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        url = 'https://example.invalid/x?a=1'
        with mock.patch.object(fetch_data, '_proxied', return_value=True):
            with mock.patch.object(fetch_data.urllib.request, 'urlopen', return_value=R(b'{"a":1}', url)):
                self.assertEqual(fetch_data.http_json(url), {'a': 1})
            err = urllib.error.HTTPError(url, 429, 'slow', {}, io.BytesIO(b''))
            with mock.patch.object(fetch_data.urllib.request, 'urlopen', side_effect=err):
                with self.assertRaises(fetch_data.FetchError) as cm:
                    fetch_data.http_json(url)
            self.assertTrue(cm.exception.blocked)
            with mock.patch.object(fetch_data.urllib.request, 'urlopen', return_value=R(b'{}', 'https://other/')):
                with self.assertRaises(fetch_data.FetchError) as cm:      # 被轉址 = 網站錯誤
                    fetch_data.http_json(url)
            self.assertEqual((cm.exception.kind, cm.exception.blocked), ('site', False))
            with mock.patch.object(fetch_data.urllib.request, 'urlopen', side_effect=urllib.error.URLError('x')):
                with self.assertRaises(fetch_data.FetchError) as cm:
                    fetch_data.http_json(url)
            self.assertEqual(cm.exception.kind, 'network')

    def test_daily_network_down(self):
        self.preload()
        with mock.patch.object(fetch_data, '_open', side_effect=OSError('x')) as m:
            with self.assertRaises(fetch_data.UserError) as cm:
                fetch_data.cmd_daily(None)
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual(m.call_count, 1)

    def test_daily_empty_cache(self):
        with self.assertRaises(fetch_data.UserError) as cm:
            fetch_data.cmd_daily(None)
        self.assertEqual(cm.exception.code, 6)
        self.assertIn('setup.bat', cm.exception.todo)

    def test_daily_tpex_missing(self):
        self.preload()
        with mock.patch.object(fetch_data, 'fetch_twse', return_value=[self.ROW]), \
                mock.patch.object(fetch_data, 'fetch_tpex', return_value=None):
            with self.assertRaises(fetch_data.UserError) as cm:
                fetch_data.cmd_daily(None)
        self.assertEqual(cm.exception.code, 5)

    def test_backfill_network_down(self):
        a = argparse.Namespace(start='20260302', end='20260306')
        with mock.patch.object(fetch_data, '_open', side_effect=OSError('x')) as m:
            with self.assertRaises(fetch_data.UserError) as cm:
                fetch_data.cmd_backfill(a)
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual(m.call_count, 1)
        self.assertTrue(fetch_data.STOP.is_set())

    def test_build_empty_cache(self):
        with self.assertRaises(fetch_data.UserError) as cm:
            fetch_data.build()
        self.assertEqual(cm.exception.code, 6)


REAL_LOG = fetch_data.log


class Clock:
    """假時鐘：_sleep 只是把時間往前撥"""

    def __init__(self):
        self.t = 0.0
        self.sleeps = []
        self.lock = threading.Lock()

    def now(self):
        return self.t

    def sleep(self, s):
        with self.lock:
            self.sleeps.append(s)
            self.t += s


def fake_clock(tc):
    c = Clock()
    for name, fn in (('_now', c.now), ('_sleep', c.sleep)):
        p = mock.patch.object(fetch_data, name, fn)
        p.start()
        tc.addCleanup(p.stop)
    return c


class FakeSite:
    """假的證交所／櫃買中心：依網址回傳格式正確的 JSON，並記下每次請求"""

    def __init__(self, holidays=(), clock=None):
        self.holidays = set(holidays)
        self.urls = []
        self.lock = threading.Lock()
        self.clock = clock

    def days(self, kind):
        out = []
        for u in self.urls:
            if kind in u:
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)
                out.append(q['date'][0].replace('/', '').replace('-', ''))
        return out

    def body(self, url):
        u = urllib.parse.urlsplit(url)
        q = urllib.parse.parse_qs(u.query)
        if 'MI_INDEX' in u.path:
            iso = q['date'][0]
            if '%s-%s-%s' % (iso[:4], iso[4:6], iso[6:]) in self.holidays:
                return {'stat': '很抱歉，沒有符合條件的資料!'}
            f = ['證券代號', '證券名稱', '成交股數', '成交筆數', '成交金額', '開盤價', '最高價', '最低價', '收盤價']
            return {'stat': 'OK', 'tables': [{'fields': f, 'data': [['1101', '台泥', '1,000', '1', '1', '10', '11', '9', '10.5']]}]}
        if 'afterTrading/otc' in u.path:
            y, m, d = q['date'][0].split('/')
            f = ['代號', '名稱', '收盤', '開盤', '最高', '最低', '成交股數']
            return {'stat': 'ok', 'tables': [{'date': '%d/%s/%s' % (int(y) - 1911, m, d), 'fields': f,
                                              'data': [['6488', '環球晶', '10.5', '10', '11', '9', '1,000']]}]}
        if 't187ap03' in u.path:
            if u.path.endswith('_L'):
                return [{'公司代號': '%04d' % (1101 + i), '公司簡稱': 'x', '產業別': '01'} for i in range(300)]
            return [{'SecuritiesCompanyCode': '%04d' % (6001 + i), 'CompanyAbbreviation': 'y', 'SecuritiesIndustryCode': '24'}
                    for i in range(300)]
        if 'TWT49U' in u.path:
            return {'stat': 'OK', 'fields': ['資料日期', '股票代號', '除權息前收盤價', '除權息參考價'],
                    'data': [['115年03月05日', '1101', '10', '9.5']]}
        if 'exDailyQ' in u.path:
            return {'stat': 'ok', 'tables': [{'fields': ['除權息日期', '代號', '除權息前收盤價', '除權息參考價'], 'data': []}]}
        raise AssertionError('不認得的網址 ' + url)

    def __call__(self, url):
        with self.lock:
            self.urls.append(url)
        return 200, {}, json.dumps(self.body(url), ensure_ascii=False).encode('utf-8')


class TestNet(TempCase):
    def setUp(self):
        super().setUp()
        isolate_net(self, sleep_fails=False)

    # ---- 規劃 ----
    def test_plan_days(self):
        start, end = dt.date(2025, 8, 20), dt.date(2026, 10, 22)
        days, weeks = fetch_data.plan_days(start, end, True)
        every = list(fetch_data.weekdays(start, end))
        self.assertEqual(dt.date.fromisoformat(days[0]).weekday(), 0)
        self.assertGreaterEqual(len(days), fetch_data.DENSE_WEEKS * 5 - 10)
        self.assertEqual(days[-1], '2026-10-22')
        seen = [iso for wk in weeks for iso in wk] + days
        self.assertEqual(sorted(seen), every)                       # 不漏、不重複
        self.assertEqual(len(seen), len(set(seen)))
        for wk in weeks:
            self.assertEqual(wk, sorted(wk, reverse=True))          # 週五在前
            self.assertEqual(len({dt.date.fromisoformat(x).isocalendar()[:2] for x in wk}), 1)
            self.assertLess(wk[0], days[0])
        self.assertEqual(weeks, sorted(weeks))                      # 舊的週在前
        self.assertEqual(weeks[0][-1], '2025-08-20')                # 第一週從 start 起算
        self.assertEqual(len(weeks[0]), 3)
        d2, w2 = fetch_data.plan_days(start, end, False)
        self.assertEqual((d2, w2), (every, []))
        d3, w3 = fetch_data.plan_days(dt.date(2026, 5, 4), end, True)   # 範圍比 34 週短：全部每天抓
        self.assertEqual((d3, w3), (list(fetch_data.weekdays(dt.date(2026, 5, 4), end)), []))

    # ---- 限速 ----
    def test_gap_per_host_fake_clock(self):
        clock = fake_clock(self)
        starts = []

        def op(url):
            starts.append((urllib.parse.urlsplit(url).netloc, clock.t))
            return 200, {}, b'{"a":1}'
        with mock.patch.object(fetch_data, '_open', side_effect=op), \
                mock.patch.object(fetch_data, 'GAP', 3.0):
            for i in range(3):
                fetch_data.get('https://a.example/x%d' % i, lambda d: d)
            self.assertEqual(clock.sleeps, [3.0, 3.0])
            fetch_data.get('https://b.example/y', lambda d: d)         # 另一個網站不必等
            self.assertEqual(clock.sleeps, [3.0, 3.0])
        self.assertEqual([t for h, t in starts if h == 'a.example'], [0.0, 3.0, 6.0])
        self.assertEqual(starts[-1], ('b.example', 6.0))

    def test_retry_waits_only_that_host(self):
        clock = fake_clock(self)
        starts = []
        seq = iter([OSError('x'), None])

        def op(url):
            host = urllib.parse.urlsplit(url).netloc
            starts.append((host, clock.t))
            if host == 'a.example':
                e = next(seq)
                if e:
                    raise e
            return 200, {}, b'{"a":1}'
        with mock.patch.object(fetch_data, '_open', side_effect=op), \
                mock.patch.object(fetch_data, 'GAP', 3.0), mock.patch.object(fetch_data, 'RETRY', (10, 30, 90)):
            fetch_data.get('https://a.example/x', lambda d: d)
            self.assertEqual(clock.sleeps, [10])
            fetch_data.get('https://b.example/y', lambda d: d)
            self.assertEqual(clock.sleeps, [10])                      # B 沒有被 A 的重試拖慢
        self.assertEqual(starts, [('a.example', 0.0), ('a.example', 10.0), ('b.example', 10.0)])
        h = fetch_data._hosts['a.example']
        self.assertEqual((h.slow, h.retries, h.count), (0.0, 1, 2))
        self.assertFalse(fetch_data.STOP.is_set())

    def test_two_blocks_stop(self):
        clock = fake_clock(self)
        with mock.patch.object(fetch_data, '_open', return_value=(429, {}, b'')) as m, \
                mock.patch.object(fetch_data, 'GAP', 3.0), mock.patch.object(fetch_data, 'RETRY', (10, 30, 90)):
            with self.assertRaises(fetch_data.FetchError) as cm:
                fetch_data.get('https://a.example/x', lambda d: d)
        self.assertTrue(cm.exception.blocked)
        self.assertEqual(m.call_count, 2)
        self.assertTrue(fetch_data.STOP.is_set())
        self.assertEqual(fetch_data._hosts['a.example'].slow, fetch_data.GAP_MAX)
        self.assertEqual(clock.sleeps, [10])
        with self.assertRaises(fetch_data._Aborted):                  # 停了就不再送
            fetch_data.get('https://b.example/x', lambda d: d)
        self.assertEqual(m.call_count, 2)

    def test_threads_one_request_per_host_and_gap(self):
        active, starts, lock = {}, {}, threading.Lock()
        over = []

        def op(url):
            host = urllib.parse.urlsplit(url).netloc
            with lock:
                active[host] = active.get(host, 0) + 1
                if active[host] > 1:
                    over.append(host)
                starts.setdefault(host, []).append(time.perf_counter())
            time.sleep(0.03)
            with lock:
                active[host] -= 1
            return 200, {}, b'{"a":1}'

        def work(host):
            for i in range(6):
                fetch_data.get('https://%s/%d' % (host, i), lambda d: d)
        t0 = time.perf_counter()
        with mock.patch.object(fetch_data, '_open', side_effect=op), mock.patch.object(fetch_data, 'GAP', 0.2):
            ts = [threading.Thread(target=work, args=(h,)) for h in ('a.example', 'b.example') for _ in range(2)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(30)
        took = time.perf_counter() - t0
        self.assertEqual(over, [])
        for host, st in starts.items():
            self.assertEqual(len(st), 12)
            self.assertGreaterEqual(min(b - a for a, b in zip(st, st[1:])), 0.19, host)
        self.assertLess(abs(starts['a.example'][0] - starts['b.example'][0]), 0.15)    # 兩個網站同時開始
        self.assertLess(took, 3.6)                                    # 不是兩個網站排隊

    # ---- 保持連線 ----
    def test_keep_alive_and_reconnect(self):
        made = []

        class Resp:
            status = 200
            will_close = False
            headers = {'Content-Encoding': 'gzip'}

            def read(self):
                return gzip.compress(b'{"a":1}')

        class Conn:
            def __init__(self, host, **kw):
                self.n, self.closed = 0, False
                made.append(self)

            def request(self, method, path, headers=None):
                self.n += 1
                assert headers['Accept-Encoding'] == 'gzip' and method == 'GET'
                if len(made) == 1 and self.n == 3:
                    raise http.client.RemoteDisconnected('stale')

            def getresponse(self):
                return Resp()

            def close(self):
                self.closed = True
        with mock.patch.object(fetch_data.http.client, 'HTTPSConnection', Conn):
            self.assertEqual(fetch_data.http_json('https://a.example/1'), {'a': 1})
            self.assertEqual(fetch_data.http_json('https://a.example/2'), {'a': 1})
            self.assertEqual(len(made), 1)                           # 兩次請求共用一條連線
            self.assertEqual(fetch_data.http_json('https://a.example/3'), {'a': 1})   # 閒置斷線：重連一次
            self.assertEqual(len(made), 2)
            self.assertTrue(made[0].closed)

    def test_disconnect_on_fresh_connection_is_network_error(self):
        class Conn:
            def __init__(self, host, **kw):
                pass

            def request(self, *a, **k):
                raise http.client.RemoteDisconnected('x')

            def close(self):
                pass
        with mock.patch.object(fetch_data.http.client, 'HTTPSConnection', Conn):
            with self.assertRaises(fetch_data.FetchError) as cm:
                fetch_data.http_json('https://a.example/1')
        self.assertEqual(cm.exception.kind, 'network')
        self.assertNotIn('a.example', fetch_data._conns)

    # ---- 整段回補 ----
    def run_small(self, site, start=dt.date(2025, 3, 3), end=dt.date(2025, 3, 28)):
        with mock.patch.object(fetch_data, 'DENSE_WEEKS', 1), mock.patch.object(fetch_data, '_open', site):
            return fetch_data.run_backfill(start, end, True)

    def test_full_backfill_counts_and_resume(self):
        fake_clock(self)
        site = FakeSite(holidays=('2025-03-19', '2025-03-14'))
        self.assertEqual(self.run_small(site), 0)
        # 密集 3/17~3/28 共 10 天、1 天休市：9*2+1；週線 3/3 週五 2 次；3/10 週五休市 1 次 + 週四 2 次；除權息 2 次
        self.assertEqual(len(site.urls), 19 + 2 + 3 + 2 + 2)       # + 類股資料（上市、上櫃各 1 次）
        self.assertEqual(sorted(site.days('MI_INDEX')).count('20250312'), 0)          # 週三不抓
        self.assertEqual(sorted(d for d in site.days('MI_INDEX') if d < '20250317'), ['20250307', '20250313', '20250314'])
        self.assertEqual(sorted(d for d in site.days('afterTrading/otc') if d < '20250317'), ['20250307', '20250313'])
        self.assertTrue(os.path.exists(os.path.join(fetch_data.CACHE, 'latest.txt')))
        self.assertTrue(os.path.exists(os.path.join(fetch_data.DATA, '1101.csv')))
        with open(os.path.join(fetch_data.CACHE, 'twse', '20250314.json'), encoding='utf-8') as f:
            self.assertTrue(json.load(f)['holiday'])
        site2 = FakeSite(holidays=('2025-03-19', '2025-03-14'))
        self.assertEqual(self.run_small(site2), 0)
        self.assertEqual(site2.urls, [])                                               # 續跑：什麼都不用再抓

    def test_weekly_friday_holiday_goes_back_only_as_needed(self):
        fake_clock(self)
        site = FakeSite(holidays=('2025-03-07', '2025-03-06'))
        self.run_small(site, end=dt.date(2025, 3, 21))
        early = sorted(d for d in site.days('MI_INDEX') if d < '20250310')
        self.assertEqual(early, ['20250305', '20250306', '20250307'])      # 週五、週四都休市 → 週三；週二不抓

    def test_blocked_backfill_exit_4_no_build(self):
        fake_clock(self)
        with mock.patch.object(fetch_data, '_open', return_value=(429, {}, b'')) as m, \
                mock.patch.object(fetch_data, 'build') as b, \
                mock.patch.object(fetch_data, 'DENSE_WEEKS', 1):
            with self.assertRaises(fetch_data.UserError) as cm:
                fetch_data.run_backfill(dt.date(2025, 3, 3), dt.date(2025, 3, 28), True)
        self.assertEqual(cm.exception.code, 4)
        self.assertEqual(m.call_count, 2)
        b.assert_not_called()
        self.assertFalse(os.path.exists(os.path.join(fetch_data.CACHE, 'latest.txt')))

    def test_structure_error_no_build(self):
        fake_clock(self)
        bad = (200, {}, json.dumps({'stat': 'OK', 'tables': [{'fields': ['x'], 'data': []}]}).encode())
        with mock.patch.object(fetch_data, '_open', return_value=bad), \
                mock.patch.object(fetch_data, 'build') as b, mock.patch.object(fetch_data, 'DENSE_WEEKS', 1):
            with self.assertRaises(fetch_data.Structure):
                fetch_data.run_backfill(dt.date(2025, 3, 3), dt.date(2025, 3, 28), True)
        b.assert_not_called()

    def test_explicit_start_is_every_day(self):
        fake_clock(self)
        site = FakeSite()
        a = argparse.Namespace(start='20250303', end='20250307')
        with mock.patch.object(fetch_data, '_open', site):
            self.assertEqual(fetch_data.cmd_backfill(a), 0)
        self.assertEqual(len(site.days('MI_INDEX')), 5)
        self.assertEqual(len(site.urls), 5 * 2 + 2 + 2)

    def test_progress_line_and_estimate(self):
        fake_clock(self)
        site = FakeSite()
        with mock.patch.object(fetch_data, 'DENSE_WEEKS', 1), mock.patch.object(fetch_data, '_open', site), \
                mock.patch.object(fetch_data, 'GAP', 3.0):
            fetch_data.run_backfill(dt.date(2025, 3, 3), dt.date(2025, 3, 28), True)
        lines = [' '.join(str(x) for x in c.args) for c in fetch_data.log.call_args_list]
        self.assertTrue(any(re.match(r'^\[\d+/12\] 2025-03-\d\d ok（週線用）  剩約 ', x) for x in lines), lines)
        self.assertTrue(any(re.match(r'^\[\d+/12\] 2025-03-\d\d ok  剩約 (\d+ 分 \d+ 秒|不到 1 分鐘)$', x) for x in lines))
        self.assertTrue(any(x.startswith('要抓 12 天、除權息 2 個檔（已有快取 0 天），預估約 ') for x in lines))
        self.assertTrue(any(x.startswith('backfill 2025-03-03 ~ 2025-03-28, 10 weekdays + 2 weeks (weekly only)') for x in lines))
        self.assertTrue(any(re.match(r'^耗時 .*；請求：證交所 \d+、櫃買中心 \d+；重試 0 次$', x) for x in lines))

    def test_ctrl_c_keeps_valid_cache_and_resumes(self):
        site = FakeSite()
        cache = fetch_data.CACHE
        timer = threading.Timer(0.6, _thread.interrupt_main)
        t0 = time.monotonic()
        try:
            with mock.patch.object(fetch_data, 'GAP', 0.05), mock.patch.object(fetch_data, '_open', site):
                timer.start()
                try:
                    fetch_data.run_backfill(dt.date(2024, 1, 1), dt.date(2024, 12, 31), False)
                    interrupted = False
                except KeyboardInterrupt:
                    interrupted = True
        finally:
            timer.cancel()
        self.assertTrue(interrupted)
        self.assertLess(time.monotonic() - t0, 8)
        files = [os.path.join(cache, m, n) for m in ('twse', 'tpex') for n in os.listdir(os.path.join(cache, m))]
        self.assertTrue(files)
        for p in files:
            self.assertFalse(p.endswith('.tmp'), p)
            with open(p, encoding='utf-8') as f:
                json.load(f)
        have = {os.path.basename(p)[:8] for p in files if '%stwse%s' % (os.sep, os.sep) in p}
        site2 = FakeSite()
        with mock.patch.object(fetch_data, '_open', site2):
            self.assertEqual(fetch_data.run_backfill(dt.date(2024, 1, 1), dt.date(2024, 12, 31), False), 0)
        self.assertTrue(have.isdisjoint(site2.days('MI_INDEX')))                       # 只補缺的
        self.assertEqual(len(have) + len(set(site2.days('MI_INDEX'))), 262)             # 2024 年有 262 個平日

    # ---- 輸出 ----
    def test_log_survives_ascii_stdout(self):
        buf = io.BytesIO()
        out = io.TextIOWrapper(buf, encoding='ascii', errors='strict')
        with mock.patch.object(sys, 'stdout', out):
            REAL_LOG('連線失敗', 'abc')
            out.flush()
        text = buf.getvalue().decode('ascii')
        self.assertIn('abc', text)
        self.assertIn('\\u9023', text)

    def test_new_messages_encodable_in_cp950(self):
        import ast
        with open(os.path.join(ROOT, 'fetch_data.py'), encoding='utf-8') as f:
            tree = ast.parse(f.read())
        doc = {id(n.body[0].value) for n in ast.walk(tree)
               if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body
               and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], 'value', None), ast.Constant)}
        bad = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc:
                try:
                    n.value.encode('cp950')
                except UnicodeEncodeError:
                    bad.append(n.value)
        self.assertEqual(bad, [])


MARK = b'rem ===== messages below'
G_DL = 'if defined TWSTOCK_TEST_DOWNLOAD goto :dlpy_stub'
G_INST = 'if defined TWSTOCK_TEST_DOWNLOAD goto :dlpy_inst_stub'
G_WG = 'if defined TWSTOCK_TEST_WINGET goto :wg_stub'
G_REG = 'if not defined TWSTOCK_TEST_REGPATH '
SEAMS = ('TWSTOCK_TEST_WINGET', 'TWSTOCK_TEST_DOWNLOAD', 'TWSTOCK_TEST_REGPATH')
WINGET_LINE = ('winget install -e --id Python.Python.3.12 --scope user '
               '--accept-package-agreements --accept-source-agreements')
INSTALLER_LINE = ('"%PYEXE%" /quiet InstallAllUsers=0 InstallLauncherAllUsers=0 PrependPath=1 Include_test=0 '
                  '/log "%HERE%\\logs\\python_install.log"')


def bat_msgs(name):
    """資料區的訊息：{KEY: [一行一行的文字]}"""
    t = readb(os.path.join(ROOT, name)).decode('utf-8')
    data = t.split(MARK.decode('ascii'), 1)[1]
    out = {}
    for m in re.finditer(r'^::([A-Z0-9_]+)\|(.*?)\r?$', data, re.M):
        out.setdefault(m.group(1), []).append(m.group(2))
    return out


def install_guard_problems(raw):
    """檢查 setup.bat 每個「真的會下載／安裝」的動作，前一行都有測試開關；回傳問題清單（空 = 沒問題）"""
    code = raw.decode('utf-8').split(MARK.decode('ascii'))[0]
    lines = code.splitlines()
    bad = []
    prev = lambda i: lines[i - 1].strip() if i else ''
    exe_lines, ver_idx, inst_lines = 0, None, 0
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.lower().startswith('rem '):
            continue
        if 'Invoke-WebRequest' in s and prev(i) != G_DL:
            bad.append('Invoke-WebRequest 前一行不是 ' + G_DL)
        if s.startswith('"%PYEXE%"'):
            exe_lines += 1
            if prev(i) != G_INST:
                bad.append('執行安裝程式前一行不是 ' + G_INST)
        if s.startswith('winget --version'):
            ver_idx = i
            if prev(i) != G_WG:
                bad.append('winget --version 前一行不是 ' + G_WG)
        elif re.match(r'^winget\b', s):
            if not s.startswith('winget install'):
                bad.append('不認得的 winget 指令：' + s)
            else:
                inst_lines += 1
                if ver_idx is None or any(x.strip().startswith(':') for x in lines[ver_idx:i]):
                    bad.append('winget install 和 winget --version 之間不能有標籤')
        if 'GetEnvironmentVariable' in s and not s.startswith(G_REG):
            bad.append('讀登錄檔 PATH 的那行要以 ' + G_REG + '開頭')
        if re.search(r'\b(Start-Process|msiexec|Start-BitsTransfer)\b', s, re.I):
            bad.append('不該出現的安裝／下載指令：' + s)
    if exe_lines != 1:
        bad.append('執行 %%PYEXE%% 的行要剛好 1 行，現在是 %d' % exe_lines)
    if inst_lines != 1:
        bad.append('winget install 要剛好 1 行，現在是 %d' % inst_lines)
    for lab in (':dlpy_stub', ':dlpy_inst_stub', ':wg_stub'):
        if lab not in [x.strip() for x in lines]:
            bad.append('找不到標籤 ' + lab)
    return bad


class TestBatFiles(unittest.TestCase):
    def raw(self, name):
        with open(os.path.join(ROOT, name), 'rb') as f:
            return f.read()

    def test_format(self):
        for name in ('run.bat', 'setup.bat', 'run_daily.bat'):
            b = self.raw(name)
            self.assertFalse(b.startswith(b'\xef\xbb\xbf'), name)
            self.assertGreater(b.count(b'\n'), 3, name)
            self.assertEqual(b.count(b'\r\n'), b.count(b'\n'), name + ' 要全是 CRLF')
            self.assertEqual(b.count(b'\r'), b.count(b'\r\n'), name)
        for name in ('run.bat', 'setup.bat'):
            b = self.raw(name)
            b[:b.index(b'chcp 65001')].decode('ascii')
            # cmd 讀含中文的 UTF-8 批次檔會偶爾切歪行：會被執行的行一律 ASCII，中文只放在檔尾資料區
            b[:b.index(MARK)].decode('ascii')

    def test_every_message_key_exists(self):
        for name in ('run.bat', 'setup.bat'):
            t = self.raw(name).decode('utf-8')
            code, data = t.split(MARK.decode('ascii'))
            keys = set(re.findall(r'^::([A-Z0-9_]+)\|', data, re.M))
            used = set(re.findall(r'call :(?:msg|msgl|msgv)\s+([A-Z0-9_]+)', code))
            used |= set(re.findall(r'call :ask_(?:yn|12)\s+([A-Z0-9_]+)', code))
            self.assertEqual(used - keys, set(), name)

    def test_run_daily_wrapper(self):
        t = self.raw('run_daily.bat').decode('ascii')
        for s in ('TWSTOCK_NOPAUSE=1', 'TWSTOCK_NO_OPEN=1', 'call "%~dp0run.bat" %*', 'exit /b %ERRORLEVEL%'):
            self.assertIn(s, t)

    def findpy(self, name):
        t = self.raw(name).decode('utf-8')
        return t[t.index('\r\n:findpy\r\n'):t.index('\r\n:nopython\r\n')]

    def test_findpy_identical(self):
        a, b = self.findpy('run.bat'), self.findpy('setup.bat')
        self.assertEqual(a, b)
        self.assertIn('sys.version_info>=(3,9)', a)

    def test_no_paren_blocks(self):
        for name in ('run.bat', 'setup.bat', 'run_daily.bat'):
            for i, line in enumerate(self.raw(name).decode('utf-8').splitlines(), 1):
                line = re.sub(r'"[^"]*"', '""', line)          # 引號裡面的括號（例如 python -c）不算
                self.assertIsNone(re.search(r'\bdo\s*\($', line), (name, i))
                self.assertIsNone(re.search(r'\)\s*else\b', line), (name, i))
                self.assertIsNone(re.match(r'^\s*\)\s*$', line), (name, i))
                self.assertIsNone(re.search(r'\bif\b.*\($', line), (name, i))

    def test_no_percent_expansion_of_user_input(self):
        t = self.raw('setup.bat').decode('utf-8')
        for v in ('IN_CAPITAL', 'IN_RISK', 'ANS'):
            self.assertNotRegex(t, '%' + v + '%')

    def test_example_config_and_gitignore(self):
        b = self.raw('設定.範例.txt')
        self.assertFalse(b.startswith(b'\xef\xbb\xbf'))
        c, err = quiet(screener.load_config, os.path.join(ROOT, '設定.範例.txt'))
        self.assertEqual(err, '')
        self.assertEqual((c['capital'], c['risk'], c['mode']), (None, 0.02, 'full'))
        self.assertTrue(c['watchlist'].endswith('自選股.csv'))
        self.assertTrue(c['output_dir'].endswith('結果'))
        lines = self.raw('.gitignore').decode('utf-8').splitlines()
        self.assertLess(lines.index('設定*.txt'), lines.index('!設定.範例.txt'))
        self.assertEqual(self.raw('.gitattributes').decode('ascii').strip(), '*.bat text eol=crlf')

    def code(self, name):
        return self.raw(name).decode('utf-8').split(MARK.decode('ascii'))[0]

    def test_install_guards_present(self):
        self.assertEqual(install_guard_problems(self.raw('setup.bat')), [])

    def test_install_guard_checker_detects_missing_guards(self):
        raw = self.raw('setup.bat')
        for guard in (G_DL, G_INST, G_WG, G_REG):
            self.assertIn(guard.encode('ascii'), raw)
        for guard in (G_DL, G_INST, G_WG):
            mutated = raw.replace((guard + '\r\n').encode('ascii'), b'')
            self.assertNotEqual(mutated, raw)
            self.assertTrue(install_guard_problems(mutated), guard)
        mutated = raw.replace(G_REG.encode('ascii') + b'for /f', b'for /f')
        self.assertTrue(install_guard_problems(mutated))
        mutated = raw.replace(b'\r\n:wg_stub\r\n', b'\r\n:wg_x\r\n')
        self.assertTrue(install_guard_problems(mutated))

    def test_only_python_org_urls(self):
        for name in ('setup.bat', 'run.bat'):
            urls = re.findall(r'https?://[^\s"\'<>]+', self.raw(name).decode('utf-8'))
            for u in urls:
                self.assertTrue(u.startswith('https://www.python.org/'), (name, u))
        url_lines = [l for l in self.code('setup.bat').splitlines() if re.search(r'https?://', l)]
        self.assertEqual(url_lines, ['set "PYURL=https://www.python.org/ftp/python/%PYVER%/%PYFILE%"'])
        self.assertEqual(self.code('run.bat').count('http'), 0)

    def test_no_forbidden_tools(self):
        for name in ('setup.bat', 'run.bat', 'run_daily.bat'):
            low = self.raw(name).decode('utf-8').lower()
            for bad in ('certutil', 'bitsadmin', 'curl', 'executionpolicy', 'bypass'):
                self.assertNotIn(bad, low, (name, bad))

    def test_exact_install_commands(self):
        lines = self.code('setup.bat').splitlines()
        self.assertEqual(lines.count(WINGET_LINE), 1)
        self.assertEqual(lines.count(INSTALLER_LINE), 1)
        self.assertEqual([l for l in lines if l.startswith('"%PYEXE%"')], [INSTALLER_LINE])
        dl = [l for l in lines if 'Invoke-WebRequest' in l]
        self.assertEqual(len(dl), 1)
        self.assertTrue(dl[0].startswith('%PS% -NoProfile -NonInteractive -Command "'))
        self.assertIn('-MaximumRedirection 0 -Uri $env:PYURL -OutFile $env:PYEXE" <nul >> "%LOG%" 2>&1', dl[0])
        self.assertIn('Get-AuthenticodeSignature', self.code('setup.bat'))
        self.assertIn('Get-FileHash -Algorithm SHA256', self.code('setup.bat'))
        self.assertIn("O=Python Software Foundation", self.code('setup.bat'))
        for l in lines:
            if l.startswith('rd ') or ' rd /s' in l:
                self.assertEqual(l.strip(), 'if exist "%PYDIR%" rd /s /q "%PYDIR%"')

    def test_powershell_lines_have_nul_stdin(self):
        n = 0
        for l in self.code('setup.bat').splitlines():
            if '%PS% -' in l:
                n += 1
                self.assertIn('<nul', l)
        self.assertGreaterEqual(n, 4)

    def test_message_vars_are_set_in_code(self):
        for name in ('setup.bat', 'run.bat'):
            code = self.code(name)
            for key, texts in bat_msgs(name).items():
                for t in texts:
                    self.assertNotRegex(t, r'[\^"]', (name, key))
                    for v in re.findall(r'!([A-Za-z0-9_]+)!', t):
                        self.assertRegex(code, r'set "%s=|set /p "%s=|call :msgv \w+ %s\b' % (v, v, v), (name, key, v))

    def test_setup_has_install_messages(self):
        m = bat_msgs('setup.bat')
        for k in ('NOPY', 'Q_INSTALL', 'INST_START', 'WG_RUN', 'WG_NONE', 'WG_FAIL', 'DLPY', 'DLPY_FAIL', 'DLPY_HASH',
                  'SHA_NA', 'DLPY_SIGOK', 'DLPY_BADSIG', 'PY_INSTALLING', 'PYINST_FAIL', 'REDETECT', 'INST_OK',
                  'INST_REOPEN', 'INST_FAIL', 'NOPY_STOP', 'NOPY_HOW', 'NOPY_TIP', 'BAD_YN', 'LOGINST', 'LOGNOINST',
                  'LOGWG', 'LOGDLPY', 'LOGPYINST', 'LOGINSTOK', 'LOGREOPEN', 'LOGINSTFAIL'):
            self.assertIn(k, m)
        self.assertIn('call :ask_yn Q_INSTALL N', self.code('setup.bat'))
        self.assertEqual(m['Q_INSTALL'], ['要不要幫你安裝？（Y/N，直接按 Enter = N）：'])

    def test_run_bat_never_installs(self):
        t = self.raw('run.bat').decode('utf-8')
        for bad in ('winget', 'Invoke-WebRequest', ':ask_yn', '/quiet'):
            self.assertNotIn(bad, t)
        self.assertTrue(any('setup.bat' in l for l in bat_msgs('run.bat')['NOPY']))
        self.assertTrue(any('要不要幫你安裝' in l for l in bat_msgs('run.bat')['NOPY']))


def decode_console(b):
    """雙擊時 chcp 65001 會生效，輸出是 UTF-8；從管線跑（沒有真的視窗）時 cmd 會用系統的 Big5 輸出，兩種都認"""
    try:
        return b.decode('utf-8')
    except UnicodeDecodeError:
        return b.decode('cp950', 'replace')


def sysroot_dir(sub):
    return os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), sub)


class _SetupRunner(TempCase):
    """把 .bat 複製到暫存資料夾，用假的 screener.py／fetch_data.py 跑；PATH 只有 System32，不會碰到真的 Python"""
    BAT = 'setup.bat'

    def setUp(self):
        super().setUp()
        self.app = os.path.join(self.tmp, 'app 資料夾 (x86)')
        self.local = os.path.join(self.tmp, 'local')
        self.tmpdir = os.path.join(self.tmp, 'tmp')
        for d in (self.app, self.local, self.tmpdir):
            os.makedirs(d)
        for n in ('setup.bat', 'run.bat'):
            with open(os.path.join(self.app, n), 'wb') as f:
                f.write(readb(os.path.join(ROOT, n)))
        for n in ('screener.py', 'fetch_data.py'):
            write(os.path.join(self.app, n), 'import sys\nsys.exit(0)\n')
        self.sys32 = sysroot_dir('System32')
        self.pydir = os.path.dirname(os.path.abspath(sys.executable))
        self.pydl = os.path.join(self.tmpdir, 'twstock-python-setup')

    def run_bat(self, answers, nopause=True, **envx):
        env = {'SystemRoot': sysroot_dir(''), 'windir': sysroot_dir(''), 'ComSpec': os.path.join(self.sys32, 'cmd.exe'),
               'PATHEXT': '.COM;.EXE;.BAT;.CMD', 'PATH': self.sys32, 'LOCALAPPDATA': self.local, 'TEMP': self.tmpdir,
               'TMP': self.tmpdir, 'USERPROFILE': self.local, 'PROCESSOR_ARCHITECTURE': 'AMD64',
               'TWSTOCK_NO_OPEN': '1', 'TWSTOCK_TEST_WINGET': 'fail', 'TWSTOCK_TEST_DOWNLOAD': 'fail',
               'TWSTOCK_TEST_REGPATH': self.sys32}
        env['SystemRoot'] = env['windir'] = os.environ.get('SystemRoot', r'C:\Windows')
        if nopause:
            env['TWSTOCK_NOPAUSE'] = '1'
        for k, v in envx.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        for k in SEAMS:
            self.assertTrue(env.get(k), '測試開關 %s 一定要設，否則可能真的下載或安裝' % k)
        # stdin 不能直接給檔案：系統的 chcp.com 會把整個檔案吞掉（雙擊時是真的視窗，不會）；
        # 給管線的話 cmd 又會一次把管線裡的東西全部讀走。所以每看到一個提問（行尾是「：」或「> 」加空白，
        # 或 pause 的「. . . 」）才寫一行答案；答案用完就當作直接按 Enter
        p = subprocess.Popen(['cmd.exe', '/d', '/c', 'call', os.path.join(self.app, self.BAT)], cwd=self.app, env=env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        chunks = []
        queue = list(answers)
        prompts = tuple(x + b' ' for x in ('：'.encode('utf-8'), '：'.encode('cp950'), b'>')) + (b'. . . ',)

        def pump():
            tail = b''
            while True:
                c = p.stdout.read1(65536)
                if not c:
                    break
                chunks.append(c)
                tail = (tail + c)[-64:]
                if tail.endswith(prompts):
                    tail = b''
                    line = queue.pop(0) if queue else ''
                    try:
                        p.stdin.write(line.encode('utf-8') + b'\r\n')
                        p.stdin.flush()
                    except OSError:
                        pass

        th = threading.Thread(target=pump, daemon=True)
        th.start()
        try:
            p.wait(timeout=180)
        except subprocess.TimeoutExpired:
            p.kill()
            raise
        th.join(30)
        for f in (p.stdin, p.stdout):
            try:
                f.close()
            except OSError:
                pass
        return p.returncode, decode_console(b''.join(chunks))

    def log_text(self):
        p = os.path.join(self.app, 'logs', 'setup.log')
        return decode_console(readb(p)) if os.path.exists(p) else ''


@unittest.skipUnless(os.name == 'nt', '需要 Windows')
class TestSetupInstallPython(_SetupRunner):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        probs = install_guard_problems(readb(os.path.join(ROOT, 'setup.bat')))
        if probs:
            raise RuntimeError('setup.bat 缺少測試開關，拒絕執行任何 .bat 以免真的安裝 Python：' + '；'.join(probs))
        if not os.path.exists(os.path.join(os.path.dirname(os.path.abspath(sys.executable)), 'python.exe')):
            raise unittest.SkipTest('找不到 python.exe')

    M = bat_msgs('setup.bat')

    def has(self, out, key, i=0):
        return self.M[key][i].split('!')[0] in out

    def found_path(self):
        return self.sys32 + ';' + self.pydir

    def finish_answers(self):
        return ['Y', '', '', '', 'N']           # 安裝 Y；三個問題用預設；不下載

    def test_N_stops_with_manual_steps(self):
        rc, out = self.run_bat(['N'])
        self.assertEqual(rc, 20, out)
        for k in ('NOPY', 'Q_INSTALL', 'NOPY_STOP', 'NOPY_HOW', 'NOPY_TIP'):
            self.assertTrue(self.has(out, k), (k, out))
        self.assertFalse(self.has(out, 'INST_START'))
        self.assertFalse(os.path.exists(self.pydl))
        self.assertIn('使用者選擇不安裝', self.log_text())

    def test_enter_means_N(self):
        rc, out = self.run_bat([''])
        self.assertEqual(rc, 20, out)
        self.assertTrue(self.has(out, 'NOPY_STOP'))
        self.assertFalse(self.has(out, 'INST_START'))

    def test_empty_stdin_means_N(self):
        rc, out = self.run_bat([])
        self.assertEqual(rc, 20, out)
        self.assertFalse(self.has(out, 'INST_START'))

    def test_garbage_answers_are_not_executed(self):
        garbage = ['&echo HACKED', '%PATH%', '!PATH!', '"', '^', 'y & echo HACKED', '>pwned.txt', 'a|b', 'yes']
        rc, out = self.run_bat(garbage + ['N'])
        self.assertEqual(rc, 20, out)
        self.assertNotIn('HACKED', out)
        for d in (self.app, self.tmp, os.getcwd()):
            self.assertFalse(os.path.exists(os.path.join(d, 'pwned.txt')), d)
        self.assertEqual(out.count(self.M['BAD_YN'][0]), len(garbage), out)
        self.assertFalse(self.has(out, 'INST_START'))

    def test_winget_ok_and_found_continues_setup(self):
        rc, out = self.run_bat(self.finish_answers(), TWSTOCK_TEST_WINGET='ok', TWSTOCK_TEST_REGPATH=self.found_path())
        self.assertEqual(rc, 0, out)
        for k in ('INST_START', 'REDETECT', 'INST_OK', 'PYFOUND', 'STEP3', 'SKIPPED'):
            self.assertTrue(self.has(out, k) if k in self.M else k in out, (k, out))
        self.assertIn('Python 3', out)
        self.assertFalse(self.has(out, 'DLPY'))
        self.assertFalse(self.has(out, 'INST_REOPEN'))
        self.assertFalse(os.path.exists(self.pydl))

    def test_winget_ok_but_not_found_asks_to_reopen(self):
        rc, out = self.run_bat(['Y'], TWSTOCK_TEST_WINGET='ok')
        self.assertEqual(rc, 20, out)
        self.assertTrue(self.has(out, 'INST_REOPEN'), out)
        self.assertTrue(self.has(out, 'INST_REOPEN', 1))
        self.assertFalse(self.has(out, 'DLPY'))
        self.assertFalse(self.has(out, 'INST_OK'))
        self.assertFalse(os.path.exists(self.pydl))

    def test_no_winget_download_ok_found(self):
        import hashlib
        rc, out = self.run_bat(self.finish_answers(), TWSTOCK_TEST_WINGET='missing', TWSTOCK_TEST_DOWNLOAD='ok',
                               TWSTOCK_TEST_REGPATH=self.found_path())
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.has(out, 'WG_NONE'))
        self.assertTrue(re.search(r'https://www\.python\.org/ftp/python/3\.12\.10/python-3\.12\.10(-amd64|-arm64)?\.exe', out), out)
        self.assertIn(hashlib.sha256(b'stub\r\n').hexdigest().upper(), out.upper())
        self.assertTrue(self.has(out, 'PY_INSTALLING'))
        self.assertTrue(self.has(out, 'INST_OK'))
        self.assertFalse(os.path.exists(self.pydl))
        self.assertTrue(self.has(out, 'PYFOUND'))

    def test_winget_fail_and_download_fail(self):
        rc, out = self.run_bat(['Y'])
        self.assertEqual(rc, 20, out)
        self.assertIn('winget 沒有裝好（代碼 1）', out)
        for k in ('DLPY_FAIL', 'INST_FAIL', 'NOPY_HOW'):
            self.assertTrue(self.has(out, k), (k, out))
        self.assertFalse(self.has(out, 'PY_INSTALLING'))
        self.assertFalse(os.path.exists(self.pydl))

    def test_bad_signature_never_runs_installer(self):
        rc, out = self.run_bat(['Y'], TWSTOCK_TEST_WINGET='missing', TWSTOCK_TEST_DOWNLOAD='badsig', TWSTOCK_TEST_REGPATH=self.found_path())
        self.assertEqual(rc, 20, out)
        self.assertTrue(self.has(out, 'DLPY_HASH'))
        self.assertTrue(self.has(out, 'DLPY_BADSIG'), out)
        self.assertTrue(self.has(out, 'INST_FAIL'))
        self.assertFalse(self.has(out, 'DLPY_SIGOK'))
        self.assertFalse(self.has(out, 'PY_INSTALLING'))
        self.assertFalse(self.has(out, 'INST_OK'))
        self.assertFalse(os.path.exists(self.pydl))

    def test_installer_failure_code(self):
        rc, out = self.run_bat(['Y'], TWSTOCK_TEST_WINGET='missing', TWSTOCK_TEST_DOWNLOAD='instfail', TWSTOCK_TEST_REGPATH=self.found_path())
        self.assertEqual(rc, 20, out)
        self.assertIn('安裝程式沒有成功（代碼 1603）', out)
        self.assertTrue(self.has(out, 'INST_FAIL'))
        self.assertFalse(self.has(out, 'INST_OK'))
        self.assertFalse(os.path.exists(self.pydl))

    def test_installer_ok_but_not_found(self):
        rc, out = self.run_bat(['Y'], TWSTOCK_TEST_DOWNLOAD='ok')
        self.assertEqual(rc, 20, out)
        self.assertTrue(self.has(out, 'PY_INSTALLING'))
        self.assertTrue(self.has(out, 'INST_REOPEN'), out)
        self.assertFalse(os.path.exists(self.pydl))

    def test_installer_url_by_arch(self):
        base = 'https://www.python.org/ftp/python/3.12.10/'
        for arch, w6432, want in (('AMD64', None, 'python-3.12.10-amd64.exe'), ('ARM64', None, 'python-3.12.10-arm64.exe'),
                                  ('x86', None, 'python-3.12.10.exe'), ('x86', 'AMD64', 'python-3.12.10-amd64.exe'),
                                  ('x86', 'ARM64', 'python-3.12.10-arm64.exe')):
            rc, out = self.run_bat(['Y'], TWSTOCK_TEST_WINGET='missing', PROCESSOR_ARCHITECTURE=arch,
                                   PROCESSOR_ARCHITEW6432=w6432)
            self.assertEqual(rc, 20, (arch, out))
            self.assertIn(base + want, out, (arch, w6432))
            self.assertNotIn(base + want + '.', out)

    def _redetect_found(self):
        rc, out = self.run_bat(self.finish_answers(), TWSTOCK_TEST_WINGET='ok')
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.has(out, 'INST_OK'), out)
        self.assertTrue(self.has(out, 'PYFOUND'))

    def test_redetect_via_py_launcher(self):
        real = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', 'Python', 'Launcher', 'py.exe')
        if not os.path.isfile(real):
            self.skipTest('這台電腦沒有 %LOCALAPPDATA%\\Programs\\Python\\Launcher\\py.exe 可以複製')
        dst = os.path.join(self.local, 'Programs', 'Python', 'Launcher', 'py.exe')
        os.makedirs(os.path.dirname(dst))
        with open(real, 'rb') as a, open(dst, 'wb') as b:
            b.write(a.read())
        ok = subprocess.run([dst, '-3', '-c', 'import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)'],
                            capture_output=True, timeout=60)
        if ok.returncode != 0:
            self.skipTest('複製出來的 py.exe 找不到 Python 3.9 以上')
        self._redetect_found()

    def test_redetect_via_per_user_python_dir(self):
        link = os.path.join(self.local, 'Programs', 'Python', 'Python312')
        os.makedirs(os.path.dirname(link))
        r = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', link, self.pydir], capture_output=True)
        if r.returncode != 0:
            self.skipTest('建不了資料夾連結（mklink /J）')
        self.addCleanup(os.rmdir, link)
        self._redetect_found()

    def test_every_stop_pauses(self):
        scenarios = [(['N'], {}), (['Y'], {'TWSTOCK_TEST_WINGET': 'ok'}), (['Y'], {}),
                     (['Y'], {'TWSTOCK_TEST_DOWNLOAD': 'badsig'}), (['Y'], {'TWSTOCK_TEST_DOWNLOAD': 'instfail'}),
                     (['Y'], {'TWSTOCK_TEST_DOWNLOAD': 'ok'})]
        for answers, envx in scenarios:
            rc1, quiet_out = self.run_bat(answers, nopause=True, **envx)
            rc2, paused_out = self.run_bat(answers, nopause=False, **envx)
            self.assertEqual((rc1, rc2), (20, 20), (answers, envx, paused_out))
            self.assertGreater(len(paused_out), len(quiet_out), (answers, envx))

    def test_python_found_never_asks_to_install(self):
        rc, out = self.run_bat(['', '', '', 'N'], TWSTOCK_PY=sys.executable)
        self.assertEqual(rc, 0, out)
        self.assertFalse(self.has(out, 'Q_INSTALL'))
        self.assertFalse(self.has(out, 'NOPY'))
        self.assertTrue(self.has(out, 'PYFOUND'))

    def test_installer_is_not_left_in_logs_or_app_dir(self):
        self.run_bat(['Y'], TWSTOCK_TEST_DOWNLOAD='ok')
        names = [n.lower() for _, _, fs in os.walk(self.app) for n in fs]
        self.assertFalse([n for n in names if n.startswith('python-3.12')], names)
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, 'twstock-python-setup')))


@unittest.skipUnless(os.name == 'nt', '需要 Windows')
class TestRunBatWithoutPython(_SetupRunner):
    BAT = 'run.bat'

    def test_points_to_setup_bat(self):
        rc, out = self.run_bat([], TWSTOCK_TODAY='20261022', TWSTOCK_DOW='4',
                               PATH=self.sys32 + ';' + os.path.join(self.sys32, 'WindowsPowerShell', 'v1.0'))
        self.assertEqual(rc, 20, out)
        self.assertIn('setup.bat', out)
        self.assertIn('要不要幫你安裝', out)
        self.assertNotIn('Y/N', out)
        self.assertFalse(os.path.exists(self.pydl))


# ---------- v1.1.4 掃描加速：結果必須和原本完全一樣 ----------
STD8 = ['date', 'open', 'high', 'low', 'close', 'volume', 'raw_close', 'name']


def rand_csv(rnd, n=None):
    """隨機產生各種寫法的 csv（標準、欄序不同、FinMind 欄名、多欄、引號、BOM、各種換行、重複日期…）"""
    n = n if n is not None else rnd.randint(0, 40)
    head = rnd.choice([STD8, STD8, STD8, STD8[:6], STD8[:6] + ['name'], STD8 + ['x'],
                       ['date', 'open', 'max', 'min', 'close', 'Trading_Volume'], STD8[:5] + ['volume', 'name', 'raw_close']])
    day = dt.date(2026, 1, 1)
    rows = []
    bad = 0.02 if rnd.random() < 0.3 else 0.0                    # 約三成的檔案會有壞資料
    for _ in range(n):
        day += dt.timedelta(days=rnd.randint(0, 2))           # 0 = 重複日期
        c = round(rnd.uniform(1, 500), rnd.randint(0, 4))
        v = rnd.choice([0, 0.0, 1, 123456, 2.5e6, '0', '-0', '1e3'])

        def num(x):
            if rnd.random() < bad:
                return rnd.choice(['', 'abc', '1,2'])
            return rnd.choice([str(x)] * 6 + [' %s' % x, '%s ' % x, '%e' % x, '1_0'])
        vals = dict(date=day.isoformat(), open=num(c), high=num(c * 1.01), max=num(c * 1.01), low=num(c * 0.99),
                    min=num(c * 0.99), close=rnd.choice([num(c)] * 10 + ['0', 'nan', 'inf']), volume=str(v),
                    Trading_Volume=str(v), raw_close=rnd.choice([str(c * 1.1), '', str(c), num(c)]),
                    name=rnd.choice(['台泥', '', 'A B', '名 稱'] + (['台,泥'] if bad else [])), x='y')
        f = [vals[h] for h in head]
        if rnd.random() < bad:
            f = f[:rnd.randint(0, len(f))]                      # 欄數不對
        rows.append(f)
    q = rnd.random() < 0.1
    cell = lambda s: '"%s"' % s if q or ',' in s else s
    lines = [','.join(head)] + [','.join(cell(s) for s in r) for r in rows]
    if rnd.random() < 0.1:
        lines.insert(rnd.randint(1, len(lines)), '')            # 空白行
    nl = rnd.choice(['\r\n', '\r\n', '\n', '\r'])
    text = nl.join(lines) + rnd.choice([nl, ''])
    raw = text.encode('utf-8')
    if rnd.random() < 0.3:
        raw = b'\xef\xbb\xbf' + raw
    if bad and rnd.random() < 0.1 and len(raw) > 60:
        raw = raw[:50] + b'\xff' + raw[50:]                     # 編碼錯誤
    return raw


def outcome(fn, *a):
    """回傳結果或例外的 repr（nan != nan，所以比 repr）"""
    try:
        return repr(('ok', fn(*a)))
    except Exception as e:      # noqa: BLE001
        return repr(('err', type(e).__name__, str(e)))


class TestFastScan(TempCase):
    def test_fast_loader_equals_load_bars_random(self):
        rnd = __import__('random').Random(20261026)
        fast = 0
        for i in range(600):
            p = write(os.path.join(self.tmp, '%04d.csv' % i), rand_csv(rnd))
            raw = readb(p)
            want = outcome(screener.load_bars, p)
            self.assertEqual(outcome(screener.load_file, p, raw), want, p)
            self.assertEqual(outcome(screener.load_file, p, None), want, p)
            try:
                fast += screener._parse_fast(raw) is not None
            except Exception:       # noqa: BLE001
                pass
        self.assertTrue(100 < fast < 500, fast)     # 快速路徑與退回 load_bars 兩邊都有測到

    def test_fast_path_used_for_standard_files(self):
        d = os.path.join(self.tmp, 'data')
        make_data(d, n=150)
        p = os.path.join(d, '1101.csv')
        with mock.patch.object(screener, 'load_bars', wraps=screener.load_bars) as lb:
            got = screener.load_file(p, readb(p))
        lb.assert_not_called()
        self.assertEqual(got, screener.load_bars(p))
        self.assertEqual(len(got[1]), 150)

    def test_fallback_triggers(self):
        good = 'date,open,high,low,close,volume,raw_close,name\r\n2026-01-02,1,2,0.5,1.5,100,1.6,台泥\r\n'
        cases = dict(
            quote=good.replace('台泥', '"台泥"'), header=good.replace('raw_close,name', 'name,raw_close'),
            alias=good.replace('high,low', 'max,min'), extra=good.replace('name\r\n', 'name,x\r\n'),
            short=good + '2026-01-03,1,2\r\n', cr=good.replace('\r\n', '\r'), nul=good.replace('台泥', '台\x00泥'),
            badnum=good.replace(',1,2,', ',x,2,'), emptyclose=good.replace(',1.5,', ',,'),
            longline=good + '2026-01-03,1,2,0.5,1.5,100,1.6,' + 'x' * 140000 + '\r\n')
        for name, text in cases.items():
            p = write(os.path.join(self.tmp, name + '.csv'), text)
            with mock.patch.object(screener, 'load_bars', wraps=screener.load_bars) as lb:
                got = outcome(screener.load_file, p, readb(p))
            lb.assert_called_once_with(p)
            self.assertEqual(got, outcome(screener.load_bars, p), name)
        p = write(os.path.join(self.tmp, 'bad.csv'), good.encode('utf-8') + b'\xff')
        with mock.patch.object(screener, 'load_bars', wraps=screener.load_bars) as lb:
            got = outcome(screener.load_file, p, readb(p))
        lb.assert_called_once_with(p)
        self.assertTrue(got.startswith("('err', 'UnicodeDecodeError'"), got)
        self.assertEqual(got, outcome(screener.load_bars, p))
        j = write(os.path.join(self.tmp, '9999.json'), json.dumps(dict(name='J', bars=[
            dict(date='2026-01-02', open=1, high=2, low=0.5, close=1.5, volume=100)])))
        self.assertIsNone(screener._read_raw(j))
        with mock.patch.object(screener, 'load_bars', wraps=screener.load_bars) as lb:
            self.assertEqual(screener.load_file(j, readb(j)), screener.load_bars(j))
        lb.assert_called_with(j)
        self.assertIsNone(screener._read_raw(os.path.join(self.tmp, 'nope.csv')))

    def test_boll_tail_matches_full(self):
        rnd = __import__('random').Random(7)
        xs = [rnd.uniform(10, 20) for _ in range(300)]
        full = screener.boll(xs)
        for s in (0, 5, 19, 20, 178, 299, 300):
            part = screener.boll(xs, start=s)
            self.assertEqual(part[0], full[0])
            for a, b in zip(part[1:], full[1:]):
                self.assertEqual(a[s:], b[s:])
                self.assertEqual(a[:s], [None] * min(s, len(xs)))

    def test_analyze_same_as_full_boll(self):
        rnd = __import__('random').Random(11)
        orig = screener.boll
        full = lambda xs, n=20, k=2, start=0: orig(xs, n, k)
        typed = 0
        for n in (79, 80, 81, 100, 121, 122, 123, 140, 141, 142, 143, 200, 260):
            for seed in range(12):
                d = dt.date(2025, 1, 6)
                bars, c = [], rnd.uniform(20, 200)
                for i in range(n):
                    flat = i < n - 3 and rnd.random() < 0.7
                    o = c * (1 + rnd.uniform(-0.004, 0.004) if flat else 1 + rnd.uniform(-0.02, 0.02))
                    c = o * (1 + (rnd.uniform(-0.004, 0.005) if flat else rnd.uniform(-0.03, 0.06)))
                    if i >= n - 2 and rnd.random() < 0.5:
                        c = o * 1.06                              # 長紅 K
                    v = rnd.randint(1000, 5000) * (4 if i >= n - 2 and rnd.random() < 0.5 else 1)
                    bars.append(dict(date=(d + dt.timedelta(days=i)).isoformat(), open=o, high=max(o, c) * 1.005,
                                     low=min(o, c) * 0.995, close=c, raw_close=c, volume=v))
                got = screener.analyze('x', 'n', bars)
                with mock.patch.object(screener, 'boll', full):
                    want = screener.analyze('x', 'n', bars)
                self.assertEqual(got, want, (n, seed))
                typed += bool(got.get('type'))
        self.assertGreater(typed, 0)

    def test_prefetch_keeps_order(self):
        rnd = __import__('random').Random(3)
        paths = ['f%03d.csv' % i for i in range(200)] + ['g.json']
        real = screener._read_raw

        def slow(p):
            time.sleep(rnd.random() * 0.002)
            return None if p.endswith('.json') else p.encode()
        with mock.patch.object(screener, '_read_raw', slow):
            got = list(screener.prefetch(paths, workers=8))
        self.assertEqual([p for p, _ in got], paths)
        self.assertEqual([r for _, r in got], [p.encode() for p in paths[:-1]] + [None])
        self.assertEqual(list(screener.prefetch([])), [])
        self.assertIs(screener._read_raw, real)
        self.assertTrue(4 <= screener.read_workers() <= 16)

    def scan_json(self, d, **kw):
        out = os.path.join(self.tmp, 'r%d.json' % len(os.listdir(self.tmp)))
        a = argparse.Namespace(data=d, config=None, list=None, capital=1000000, risk=None, out=out + '.md', json=out,
                               txt=None, html=None, csv=None, out_dir=None, open=False)
        quiet(screener.cmd_scan, a)
        return readb(out)

    def test_scan_output_independent_of_threads(self):
        d = os.path.join(self.tmp, 'data')
        make_data(d, codes=tuple(str(1100 + i) for i in range(40)), n=160)
        write(os.path.join(d, '9001.csv'), 'date,open,max,min,close,Trading_Volume\n2026-01-02,1,2,0.5,1.5,100\n')
        base = self.scan_json(d)
        with mock.patch.object(screener, 'read_workers', lambda: 1):
            self.assertEqual(self.scan_json(d), base)
        orig = screener.boll
        with mock.patch.object(screener, 'load_file', lambda p, raw=None: screener.load_bars(p)), \
                mock.patch.object(screener, 'boll', lambda xs, n=20, k=2, start=0: orig(xs, n, k)):
            self.assertEqual(self.scan_json(d), base)        # 舊做法：逐檔 load_bars、布林全部算
        codes = [r['code'] for r in json.loads(base)]
        self.assertEqual(codes, sorted(codes))

    def test_scan_unreadable_file_same_error(self):
        d = os.path.join(self.tmp, 'data')
        make_data(d)
        bad = write(os.path.join(d, '2000.csv'), 'date,open,high,low,close,volume\r\n'.encode() + b'\xff\r\n')
        with self.assertRaises(UnicodeDecodeError) as cm:
            screener.load_bars(bad)
        want = (type(cm.exception).__name__, str(cm.exception))
        r = subprocess.run([sys.executable, os.path.join(ROOT, 'screener.py'), 'scan', '--data', d],
                           cwd=self.tmp, env=clean_env(PYTHONUTF8='1'), capture_output=True, timeout=120)
        self.assertEqual(r.returncode, 1)
        self.assertIn('程式發生未預期的錯誤：%s: %s' % want, r.stderr.decode('utf-8'))


# ---------- 類股（sector）與頁面顏色切換 ----------
def sec_results(sectors):
    """fake_results 加上類股：sectors = {代號: 標籤}；沒列到的給空字串"""
    res = fake_results()
    for r in res:
        r['sector'] = sectors.get(r['code'], '')
    return res


class TestSectorTables(unittest.TestCase):
    def test_trim_rules(self):
        t = screener.trim_industry
        for official, want in (('半導體業', '半導體'), ('水泥工業', '水泥'), ('其他業', '其他'), ('農業科技業', '農業科技'),
                               ('綜合', '綜合'), ('工業', '工業'), ('業', '業'), ('電腦及週邊設備業', '電腦週邊'),
                               ('觀光事業', '觀光餐旅'), ('觀光餐旅', '觀光餐旅'), ('其他電子業', '其他電子'), ('電機機械', '電機機械')):
            self.assertEqual(t(official), want, official)

    def test_label_rules(self):
        import share
        labels = list(screener.SECTOR_ORDER)
        self.assertEqual(len(labels), len(set(labels)))
        self.assertEqual(set(labels), set(screener.SECTOR_LABELS))
        groups = {}
        for l in labels:
            self.assertTrue(share.SECTOR_RE.fullmatch(l), l)
            self.assertIsNone(re.search(r'[0-9]', l), l)
            self.assertEqual(share._nf(l), l)
            for w in share.FORBIDDEN_WORDS:
                self.assertNotIn(w, l)
            groups.setdefault(l.split('-')[0], []).append(l)
        for g, ls in groups.items():
            if len(ls) > 1:
                self.assertNotIn(g, labels, g)              # 大類名稱（篩選選單的值）不和任何標籤撞名
        self.assertEqual(screener.SECTOR_BY_CODE['24'], '科技-半導體')
        self.assertEqual(screener.SECTOR_BY_CODE['17'], '金融')
        self.assertEqual(screener.SECTOR_BY_CODE['15'], '航運')
        self.assertEqual(screener.SECTOR_BY_CODE['34'], '科技-電子商務')
        self.assertEqual(screener.SECTOR_BY_CODE['32'], '傳產-文化創意')
        self.assertEqual(screener.SECTOR_BY_CODE['16'], '傳產-觀光餐旅')

    def test_sector_of(self):
        rows = {'2330': '24', '2603': '15', '3293': '32', '1101': '水泥工業', '2882': '金融保險業', '5000': '99', '5001': '1',
                '5002': '半導體業', '5003': '不存在的業'}
        f = screener.sector_of
        self.assertEqual(f('2330', '台積電', rows), '科技-半導體')
        self.assertEqual(f('2603', '長榮', rows), '航運')
        self.assertEqual(f('3293', '鈊象', rows), '傳產-文化創意')
        self.assertEqual(f('1101', '台泥', rows), '傳產-水泥')
        self.assertEqual(f('2882', '國泰金', rows), '金融')
        self.assertEqual(f('5001', 'x', rows), '傳產-水泥')                        # 1 位數也認得
        self.assertEqual(f('5002', 'x', rows), '科技-半導體')
        for code in ('5000', '5003', '9999'):
            self.assertEqual(f(code, 'x', rows), '', code)
        self.assertEqual(f('2330', '台積電', {}), '')
        self.assertEqual(f('2330', '台積電', None), '')
        self.assertEqual(f('0050', '元大台灣50', {'0050': '24'}), 'ETF-市值型')       # ETF 不看快取

    def test_etf_types(self):
        want = [
            ('00631L', '元大台灣50正2', 'ETF-槓桿反向'), ('00632R', '元大台灣50反1', 'ETF-槓桿反向'),
            ('00680L', '元大美債20正2', 'ETF-槓桿反向'),                       # 槓桿反向勝過債券
            ('00719B', '元大美債1-3', 'ETF-債券'), ('00679B', '', 'ETF-債券'), ('00687C', '國泰20年美債+櫃U', 'ETF-債券'),
            ('00635U', '期元大S&P黃金', 'ETF-商品期貨'), ('009832', '兆豐美國黃金', 'ETF-商品期貨'),
            ('00400A', '主動國泰動能高息', 'ETF-主動式'), ('00985A', '主動野村台灣50', 'ETF-主動式'),
            ('00981D', '主動中信非投等債', 'ETF-主動式'),                      # D 尾碼：主動式債券，算主動式
            ('00980T', '平衡凱基美國TOP', 'ETF-主動式'),                        # T 尾碼：平衡型，算主動式
            ('00999X', '主動測試', 'ETF-主動式'),                               # 尾碼不認得也看名稱
            ('0056', '元大高股息', 'ETF-高股息'), ('00940', '元大台灣價值高息', 'ETF-高股息'),
            ('00878', '國泰永續高股息', 'ETF-高股息'),                          # 高股息勝過永續
            ('00932', '兆豐永續高息等權', 'ETF-高股息'),                        # 高股息勝過永續、策略
            ('00929', '復華台灣科技優息', 'ETF-科技'), ('00927', '群益半導體收益', 'ETF-科技'),   # ETF_OVERRIDE：科技為主的高息
            ('00943', '兆豐電子高息等權', 'ETF-科技'), ('00946', '群益科技高息成長', 'ETF-科技'),
            ('00962', '台新AI優息動能', 'ETF-科技'), ('00896', '中信綠能及電動車', 'ETF-科技'),
            ('009805', '台新美國電力基建', 'ETF-主題'),                          # 電力基建：公用事業，不算科技
            ('00903', '富邦元宇宙', 'ETF-科技'),
            ('00850', '元大臺灣ESG永續', 'ETF-永續'), ('00923', '群益台ＥＳＧ低碳50', 'ETF-永續'),
            ('00897', '富邦基因免疫生技', 'ETF-主題'), ('00965', '元大航太防衛科技', 'ETF-主題'),      # 主題勝過科技
            ('009817', '國泰日本不動產', 'ETF-主題'), ('00908', '富邦入息REITs+', 'ETF-主題'),
            ('0055', '元大MSCI金融', 'ETF-金融'), ('009825', '聯邦美國金融創新', 'ETF-金融'),
            ('0052', '富邦科技', 'ETF-科技'), ('00881', '富邦台灣科技', 'ETF-科技'),                   # 有主題字就不是市值型
            ('00770', '國泰北美科技', 'ETF-科技'), ('00830', '國泰費城半導體', 'ETF-科技'),            # 主題勝過地區
            ('00952', '凱基台灣AI50', 'ETF-科技'), ('00757', '統一ＦＡＮＧ＋', 'ETF-科技'),
            ('00646', '元大Ｓ＆Ｐ５００', 'ETF-海外'), ('00662', '富邦NASDAQ', 'ETF-海外'),           # 地區勝過市值型
            ('009818', '華南永昌NASDAQxT', 'ETF-海外'), ('00645', '富邦日本', 'ETF-海外'),
            ('00639', '富邦深100', 'ETF-海外'), ('00916', '國泰全球品牌50', 'ETF-海外'),
            ('0050', '元大台灣50', 'ETF-市值型'), ('006208', '富邦台50', 'ETF-市值型'), ('0051', '元大中型100', 'ETF-市值型'),
            ('00690', '兆豐藍籌30', 'ETF-市值型'), ('009810', '玉山全球藍籌100', 'ETF-海外'),       # 地區勝過藍籌
            ('00905', 'FT臺灣Smart', 'ETF-策略'),                               # Smart 不是 AI，也不是科技
            ('00921', '兆豐龍頭等權重', 'ETF-策略'), ('009803', '玉山市值動能50', 'ETF-策略'),      # 策略勝過市值
            ('00733', '富邦臺灣中小', 'ETF-策略'), ('00643', '群益深証中小', 'ETF-海外'),
            ('00728', '第一金工業30', 'ETF-其他'), ('009804', '聯邦台精彩50', 'ETF-其他'),
            ('00999', 'TAIWAN', 'ETF-其他'),                                    # AI 前後緊接大寫英文字母不算
            ('00xxx', None, 'ETF-其他'), ('', '', 'ETF-其他'),
        ]
        self.assertGreaterEqual(len(want), 25)
        for code, name, label in want:
            self.assertEqual(screener.etf_type(code, name), label, (code, name))
            self.assertIn(label, screener.ETF_LABELS)
        self.assertEqual(set(l for _, _, l in want), set(screener.ETF_LABELS))      # 每一類都有例子

    def test_etf_label_set(self):
        import share
        self.assertEqual(screener.ETF_LABELS, ('ETF-槓桿反向', 'ETF-主動式', 'ETF-債券', 'ETF-商品期貨', 'ETF-高股息', 'ETF-永續',
                                               'ETF-主題', 'ETF-金融', 'ETF-科技', 'ETF-海外', 'ETF-策略', 'ETF-市值型', 'ETF-其他'))
        self.assertEqual(screener.SECTOR_ORDER[-len(screener.ETF_LABELS):], screener.ETF_LABELS)
        for l in screener.ETF_LABELS:
            self.assertTrue(share.SECTOR_RE.fullmatch(l), l)
            self.assertNotEqual(l, 'ETF')
            self.assertLessEqual(len(l), 8, l)                     # 選單在手機上不會太寬
            self.assertTrue(re.fullmatch(r'ETF-[\u4e00-\u9fff]{2,4}', l), l)      # 除了開頭的 ETF 沒有英文字母和數字
            for w in share.FORBIDDEN_WORDS:
                self.assertNotIn(w, l)
        for label, letters, words in screener.ETF_TYPES:
            self.assertTrue(words, label)
            self.assertTrue(all(c.isupper() for c in letters), label)


class TestIndustryCache(TempCase):
    def put(self, mkt, rows=None, **k):
        d = os.path.join(self.tmp, 'cache', 'industry')
        j = {'v': 1, 'rows': {'1101': '01'} if rows is None else rows, 'fetched': '2026-10-20', 'tried': '2026-10-20',
             'stale': False, 'fails': 0}
        j.update(k)
        write(os.path.join(d, mkt + '.json'), json.dumps(j))
        return d

    def test_load_and_info(self):
        d = os.path.join(self.tmp, 'cache', 'industry')
        ind = screener.load_industry(d)
        self.assertEqual((ind['have'], ind['rows']), (0, {}))
        self.assertEqual(screener.sector_info(ind, '2026-10-22'), dict(status='none', fetched=''))
        self.put('twse')
        ind = screener.load_industry(d)                                           # 只有一邊
        self.assertEqual(screener.sector_info(ind, '2026-10-22')['status'], 'stale')
        self.put('tpex', rows={'6488': '24'}, fetched='2026-10-18')
        ind = screener.load_industry(d)
        self.assertEqual((ind['have'], ind['fetched'], sorted(ind['rows'])), (2, '2026-10-18', ['1101', '6488']))
        self.assertEqual(screener.sector_info(ind, '2026-10-22'), dict(status='ok', fetched='2026-10-18'))
        self.assertEqual(screener.sector_info(ind, '2026-11-01')['status'], 'ok')           # 剛好 14 天
        self.assertEqual(screener.sector_info(ind, '2026-11-02')['status'], 'stale')        # 15 天
        self.assertEqual(screener.sector_info(ind, '')['status'], 'ok')
        self.put('tpex', rows={'6488': '24'}, stale=True)
        self.assertEqual(screener.sector_info(screener.load_industry(d), '2026-10-22')['status'], 'stale')

    def test_broken_files_never_raise(self):
        d = self.put('twse')
        for bad in ('not json', '[]', '{"v":1}', '{"rows":[],"fetched":"2026-10-20"}', '{"rows":{"1101":"01"},"fetched":"x"}',
                    '\x00\x01', '{"rows":{}}'):
            write(os.path.join(d, 'tpex.json'), bad)
            ind = screener.load_industry(d)
            self.assertEqual(ind['have'], 1, bad)
            self.assertEqual(screener.sector_info(ind, '2026-10-22')['status'], 'stale', bad)
        write(os.path.join(d, 'tpex.json'), b'\xff\xfe\x00')
        self.assertEqual(screener.load_industry(d)['have'], 1)

    def test_industry_dir_follows_data_dir(self):
        d = screener.industry_dir(os.path.join(self.tmp, 'data'))
        self.assertEqual(d, os.path.join(self.tmp, 'cache', 'industry'))

    def test_attach_sectors_is_last_key_and_error_rows(self):
        res = fake_results()
        res.append(fake_rec('0050', '元大台灣50', 150.0, 0.1, 1.0))
        screener.attach_sectors(res, {'1101': '01', '2330': '24'})
        self.assertEqual([r['sector'] for r in res],
                         ['傳產-水泥', '科技-半導體', 'ETF-海外', '', '', '', '', 'ETF-市值型'])
        for r in res:
            self.assertEqual(list(r)[-1], 'sector')
        screener.attach_markets(res, {'1101': '上市', '0050': '上市', '6488': '上櫃'})
        for r in res:
            self.assertEqual(list(r)[-2:], ['sector', 'market'])
        self.assertEqual({r['code']: r['market'] for r in res}['1101'], '上市')
        self.assertEqual({r['code']: r['market'] for r in res}['2330'], '')
        screener.attach_markets(res, None)
        self.assertTrue(all(r['market'] == '' for r in res))

    def test_load_market(self):
        m = screener.load_market(os.path.join(self.tmp, 'data'))         # 沒有 cache 資料夾
        self.assertEqual(m, {})
        c = os.path.join(self.tmp, 'cache')
        write(os.path.join(c, 'twse', '20261029.json'), json.dumps({'date': '2026-10-29', 'rows': [['1101', '台泥'], ['0099', 'x']]}))
        write(os.path.join(c, 'twse', '20261030.json'), json.dumps({'date': '2026-10-30', 'rows': [['1101', '台泥'], ['0050', '元大台灣50']]}))
        write(os.path.join(c, 'twse', '20261031.json'), json.dumps({'date': '2026-10-31', 'rows': None}))      # 休市
        write(os.path.join(c, 'tpex', '20261030.json'), 'not json')
        write(os.path.join(c, 'tpex', '20261029.json'), json.dumps({'date': '2026-10-29', 'rows': [['00679B', 'y'], ['1101', 'z']]}))
        write(os.path.join(c, 'tpex', 'notes.json'), json.dumps({'rows': [['9999', 'q']]}))
        write(os.path.join(c, 'industry', 'tpex.json'), json.dumps({'rows': {'6488': '24', '1101': '01'}}))
        write(os.path.join(c, 'industry', 'twse.json'), '\x00')
        before = {os.path.join(r, f): readb(os.path.join(r, f)) for r, _, fs in os.walk(c) for f in fs}
        m = screener.load_market(os.path.join(self.tmp, 'data'))
        self.assertEqual(m, {'1101': '上市', '0050': '上市', '00679B': '上櫃', '6488': '上櫃'})       # 最新一天有資料的快照；上市優先
        self.assertEqual({os.path.join(r, f): readb(os.path.join(r, f)) for r, _, fs in os.walk(c) for f in fs}, before)


class TestSectorScan(TempCase):
    def run_cli(self, *args):
        env = clean_env(PYTHONUTF8='1', TWSTOCK_NO_OPEN='1')
        return subprocess.run([sys.executable, os.path.join(ROOT, 'screener.py')] + list(args), cwd=self.tmp, env=env,
                              capture_output=True, timeout=120)

    def test_scan_with_and_without_cache(self):
        d = os.path.join(self.tmp, 'data')
        tag = make_data(d, ('1101', '2330', '0050'))
        o1, o2 = os.path.join(self.tmp, 'o1'), os.path.join(self.tmp, 'o2')
        r1 = self.run_cli('scan', '--data', d, '--out-dir', o1, '--capital', '1000000')
        self.assertEqual(r1.returncode, 0, r1.stderr.decode('utf-8', 'replace'))
        self.assertNotIn('類股', r1.stderr.decode('utf-8'))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'cache')))            # scan 不會建立或寫入快取資料夾
        cdir = os.path.join(self.tmp, 'cache', 'industry')
        mk = lambda rows: json.dumps({'v': 1, 'rows': rows, 'fetched': '2026-09-20', 'tried': '2026-09-20', 'stale': False, 'fails': 0})
        write(os.path.join(cdir, 'twse.json'), mk({'1101': '01', '2330': '24'}))
        write(os.path.join(cdir, 'tpex.json'), mk({'6488': '24'}))
        before = {n: readb(os.path.join(cdir, n)) for n in os.listdir(cdir)}
        r2 = self.run_cli('scan', '--data', d, '--out-dir', o2, '--capital', '1000000')
        self.assertEqual(r2.returncode, 0)
        self.assertEqual({n: readb(os.path.join(cdir, n)) for n in os.listdir(cdir)}, before)
        for ext in ('md', 'txt'):
            self.assertEqual(readb(os.path.join(o1, '盤後選股_%s.%s' % (tag, ext))), readb(os.path.join(o2, '盤後選股_%s.%s' % (tag, ext))))
        j1 = json.loads(readb(os.path.join(o1, '盤後選股_%s.json' % tag)).decode('utf-8'))
        j2 = json.loads(readb(os.path.join(o2, '盤後選股_%s.json' % tag)).decode('utf-8'))
        self.assertEqual(len(j1), 3)
        for a, b in zip(j1, j2):
            self.assertEqual(list(a)[-2:], ['sector', 'market'])
            self.assertEqual(list(b)[-2:], ['sector', 'market'])
            self.assertEqual(list(a), list(b))
            self.assertEqual({k: v for k, v in a.items() if k not in ('sector', 'market')},
                             {k: v for k, v in b.items() if k not in ('sector', 'market')})
        self.assertEqual({r['code']: r['market'] for r in j2}, {'1101': '上市', '2330': '上市', '0050': ''})
        self.assertTrue(all(r['market'] == '' for r in j1))
        self.assertEqual({r['code']: r['sector'] for r in j2}['1101'], '傳產-水泥')
        self.assertEqual({r['code']: r['sector'] for r in j2}['2330'], '科技-半導體')
        self.assertEqual({r['code']: r['sector'] for r in j1}['1101'], '')
        for o in (o1, o2):
            rows = list(csv.reader(io.StringIO(readb(os.path.join(o, '盤後選股_%s.csv' % tag)).decode('utf-8-sig'), newline='')))
            self.assertEqual(rows[0][-2:], ['類股', '市場'])
            self.assertTrue(all(len(x) == len(rows[0]) for x in rows))
        h1 = readb(os.path.join(o1, '盤後選股_%s.html' % tag)).decode('utf-8')
        h2 = readb(os.path.join(o2, '盤後選股_%s.html' % tag)).decode('utf-8')
        self.assertNotIn('id="csec"', h1)
        self.assertNotIn('類股資料未更新', h1)
        self.assertNotIn('類股資料未更新', h2)             # 資料日期前 14 天內更新的不算舊


class TestSectorRender(unittest.TestCase):
    def page(self, sectors, status='ok', fetched='2026-10-20'):
        meta = meta_for(sector_info={'status': status, 'fetched': fetched})
        return screener.render_html(sec_results(sectors), False, meta)

    def test_labels_filter_and_summary(self):
        h = self.page({'1101': '傳產-水泥', '2330': '科技-半導體', '00646': 'ETF-市值型', '6874': '科技-光電'})
        self.assertIn('data-sec="科技-半導體"', h)
        self.assertIn('<span class="sec">傳產-水泥</span>', h)
        self.assertIn('<label class="fld" for="csec">類股<select id="csec">', h)
        i = h.index('id="csec"')
        sel = h[i:h.index('</select>', i)]
        opts = re.findall(r'<option value="([^"]*)">([^<]*)</option>', sel)
        self.assertEqual(opts[0], ('all', '全部 4'))
        self.assertIn(('科技', '科技（全部）2'), opts)
        self.assertIn(('科技-半導體', '科技-半導體 1'), opts)
        self.assertIn(('傳產-水泥', '傳產-水泥 1'), opts)
        self.assertIn(('ETF-市值型', 'ETF-市值型 1'), opts)
        self.assertNotIn('傳產（全部）', sel)                      # 只有 1 個標籤的大類不放「全部」
        self.assertNotIn('value="none"', sel)
        self.assertIn('類股：科技-半導體 1 檔・科技-光電 1 檔・傳產-水泥 1 檔・ETF-市值型 1 檔</p>', h)
        self.assertNotIn('類股資料未更新', h)
        self.assertIn("csec.value", h)

    def test_unclassified_and_etf_group(self):
        h = self.page({'1101': '傳產-水泥', '00646': 'ETF-市值型', '2330': 'ETF-高股息'})
        sel = h[h.index('id="csec"'):]
        sel = sel[:sel.index('</select>')]
        self.assertIn('<option value="ETF">ETF（全部）2</option>', sel)
        self.assertIn('<option value="none">未分類 1</option>', sel)
        self.assertIn('・未分類 1 檔</p>', h)

    def test_none_hides_ui_but_keeps_etf_label(self):
        h = self.page({'00646': 'ETF-市值型'}, status='none', fetched='')
        self.assertNotIn('id="csec"', h)
        self.assertNotIn('<p class="cap14">類股：', h)
        self.assertIn('data-sec="ETF-市值型"', h)
        self.assertIn('<span class="sec">ETF-市值型</span>', h)

    def test_stale_note(self):
        h = self.page({'1101': '傳產-水泥'}, status='stale', fetched='2026-09-01')
        self.assertIn('類股資料未更新（上次更新 2026-09-01），分類可能不是最新的。', h)
        h = self.page({'1101': '傳產-水泥'}, status='stale', fetched='')
        self.assertIn('類股資料未更新，分類可能不是最新的。', h)

    def test_escaping(self):
        h = self.page({'1101': '<b>x</b>', '2330': '"><i>'})
        self.assertNotIn('<b>x</b>', h)
        self.assertNotIn('"><i>', h)
        self.assertIn('&lt;b&gt;x&lt;/b&gt;', h)

    def test_stage_json_and_watch(self):
        res = sec_results({'1101': '傳產-水泥', '2454': '科技-IC設計', '1216': '傳產-食品'})
        h = screener.render_html(res, False, meta_for(sector_info={'status': 'ok', 'fetched': '2026-10-20'}))
        m = re.search(r'<script type="application/json" id="stage-data">(.*?)</script>', h)
        pack = json.loads(m.group(1))
        self.assertEqual(pack['s'], ['', '傳產-水泥', '傳產-食品', '科技-IC設計'])
        by = {r[0]: r[7] for r in pack['r']}
        self.assertEqual((by['1101'], by['1216'], by['2330']), (1, 2, 0))
        self.assertIn('<span class="name">聯發科</span><span class="sec">科技-IC設計</span><span class="kd">個股</span></th>', h)     # 觀察名單
        self.assertIn('<th scope="col" class="lo"><button type="button" class="sb" data-key="2">類股', h)

    def test_old_results_without_sector_still_render(self):
        h = screener.render_html(fake_results(), False, meta_for())
        self.assertNotIn('id="csec"', h)
        self.assertIn('data-sec=""', h)
        self.assertIn('data-mkt=""', h)

    def test_etf_sub_labels_grouped_in_select(self):
        h = self.page({'1101': '傳產-水泥', '00646': 'ETF-海外', '2330': 'ETF-主動式', '6874': 'ETF-科技'})
        sel = h[h.index('id="csec"'):]
        sel = sel[:sel.index('</select>')]
        opts = re.findall(r'<option value="([^"]*)">([^<]*)</option>', sel)
        vals = [v for v, _ in opts]
        self.assertEqual(vals[vals.index('ETF'):], ['ETF', 'ETF-主動式', 'ETF-科技', 'ETF-海外'])
        self.assertIn(('ETF', 'ETF（全部）3'), opts)
        self.assertTrue(all(len(t) <= 12 for _, t in opts), opts)

    def test_kind_market_chip(self):
        res = sec_results({'1101': '傳產-水泥', '00646': 'ETF-海外'})
        screener.attach_markets(res, {'1101': '上市', '00646': '上櫃'})
        h = screener.render_html(res, False, meta_for(sector_info={'status': 'ok', 'fetched': '2026-10-20'}))
        self.assertIn('<span class="sec">傳產-水泥</span><span class="kd">個股·上市</span>', h)
        self.assertIn('<span class="sec">ETF-海外</span><span class="kd">ETF·上櫃</span>', h)
        self.assertIn('data-mkt="上市"', h)
        self.assertIn('<span class="kd">個股</span>', h)              # 查不到市場只寫個股／ETF
        self.assertIn('.kd{', h)


class TestThemeToggle(unittest.TestCase):
    def test_nav_buttons(self):
        h = screener.render_html(fake_results(), False, meta_for())
        nav = h[h.index('<div class="topnav">'):h.index('<main>')]
        self.assertIn('role="group"', nav)
        self.assertEqual(len(re.findall(r'<button type="button" class="tm" data-set="(?:auto|light|dark)" aria-pressed="(?:true|false)">', nav)), 3)
        self.assertIn('data-set="auto" aria-pressed="true">自動<', nav)
        self.assertIn('>淺色<', nav)
        self.assertIn('>深色<', nav)
        self.assertIn('資料日期 2026-10-22', nav)

    def test_boot_script_in_head_is_guarded(self):
        h = screener.render_html(fake_results(), False, meta_for())
        head = h[:h.index('</head>')]
        self.assertEqual(head.count('<script>'), 1)
        self.assertIn(screener.THEME_BOOT, head)
        self.assertIn('try{', screener.THEME_BOOT)
        self.assertIn('catch(e){}', screener.THEME_BOOT)
        self.assertLess(head.index('<script>'), head.index('<title>'))
        self.assertEqual(h.count('twss-theme'), 2)                 # 開機腳本一次、頁面程式一次
        self.assertIn("var TK = 'twss-theme';", screener.HTML_JS)

    def test_every_storage_line_is_in_try(self):
        lines = [x for x in screener.HTML_JS.split('\n') if 'localStorage' in x]
        self.assertTrue(lines)
        for x in lines:
            self.assertIn('try {', x)
            self.assertIn('catch', x)
            self.assertFalse(re.search(r'\s//', x), x)
        self.assertIn('localStorage', screener.THEME_BOOT)

    def test_contrast(self):
        P = screener.PALETTE
        for name in ('light', 'dark'):
            p = P[name]
            self.assertGreaterEqual(wcag_ratio(p['ink2'], p['nav']), 7, name)
            self.assertGreaterEqual(wcag_ratio(p['page'], p['ink']), 7, name)
            self.assertGreaterEqual(wcag_ratio(p['accent'], p['nav']), 3, name)
            self.assertGreaterEqual(wcag_ratio(p['ink2'], p['page']), 7, name)
            self.assertGreaterEqual(wcag_ratio(p['ink3'], p['nav']), 4.5, name)    # 主題切換鈕的外框

    def test_css_rules(self):
        css = screener.HTML_CSS
        self.assertLess(css.index('.tm:hover'), css.index('.tm[aria-pressed="true"]'))
        # 點了馬上有反應：不用轉場動畫；可點的東西關掉雙擊縮放的等待
        self.assertNotRegex(css, r'transition|animation|@keyframes')
        for sel in ('.tm{', '.sb{', '.seg span{', '.btn{', '.chb{', 'details>summary{'):
            rule = css[css.index(sel):]
            self.assertIn('touch-action:manipulation', rule[:rule.index('}')], sel)
        # 有意義的框線用實色 ink3，不用半透明的 hairline
        for sel in ('.theme{', '.kd{', '.seg span{', '.fld input,.fld select{', '.pb-track{'):
            rule = css[css.index(sel):]
            self.assertIn('var(--ink3)', rule[:rule.index('}')], sel)
        self.assertIn('Intl.Collator', screener.HTML_JS)
        self.assertIn('.navdate{display:none}', css[css.index('@media (max-width:734px)'):])
        self.assertNotRegex(css, r'(?<![A-Za-z-])lh(?![A-Za-z-])')

    def test_screener_has_no_network_imports(self):
        with open(os.path.join(ROOT, 'screener.py'), encoding='utf-8') as f:
            src = f.read()
        self.assertIsNone(re.search(r'^\s*(?:import|from)\s+(?:urllib|http|socket|ssl)\b', src, re.M))


class TestIndustryFetch(TempCase):
    ROW = TestFetch.ROW

    def setUp(self):
        super().setUp()
        isolate_net(self, sleep_fails=False, RETRY=(10, 30, 90))
        self.clock = fake_clock(self)

    def industry_log(self):
        return [' '.join(str(x) for x in c.args) for c in fetch_data.log.call_args_list]

    def test_industry_due(self):
        due = fetch_data.industry_due
        D = dt.date(2026, 10, 1)
        ok = {'tried': D.isoformat(), 'stale': False, 'fails': 0}
        self.assertTrue(due(None, D))
        self.assertTrue(due({}, D))
        self.assertTrue(due({'tried': 'x'}, D))
        self.assertFalse(due(ok, D))
        self.assertFalse(due(ok, D + dt.timedelta(days=6)))
        self.assertTrue(due(ok, D + dt.timedelta(days=7)))
        for fails, wait in ((1, 1), (2, 2), (3, 4), (4, 7), (9, 7)):
            st = {'tried': D.isoformat(), 'stale': True, 'fails': fails}
            self.assertFalse(due(st, D + dt.timedelta(days=wait - 1)), fails)
            self.assertTrue(due(st, D + dt.timedelta(days=wait)), fails)

    def test_refresh_success_and_weekly(self):
        site = FakeSite()
        D = dt.date(2026, 10, 1)
        with mock.patch.object(fetch_data, '_open', site):
            fetch_data.refresh_industry(D)
            self.assertEqual(len(site.urls), 2)
            fetch_data.refresh_industry(D + dt.timedelta(days=6))
            self.assertEqual(len(site.urls), 2)
            fetch_data.refresh_industry(D + dt.timedelta(days=7))
            self.assertEqual(len(site.urls), 4)
        j = fetch_data.read_industry('twse')
        self.assertEqual((j['fetched'], j['tried'], j['stale'], j['fails']), ('2026-10-08',) * 2 + (False, 0))
        self.assertEqual(len(j['rows']), 300)
        raw = readb(fetch_data.ind_path('tpex')).decode('utf-8')
        self.assertNotIn('Chairman', raw)                       # 只存 代號 -> 產業別
        self.assertEqual(list(json.loads(raw)), sorted(json.loads(raw)))
        self.assertFalse([n for n in os.listdir(os.path.dirname(fetch_data.ind_path('twse'))) if n.endswith('.tmp')])

    def test_failure_keeps_old_rows_and_backs_off(self):
        site = FakeSite()
        D = dt.date(2026, 10, 1)
        with mock.patch.object(fetch_data, '_open', site):
            fetch_data.refresh_industry(D)
        old = fetch_data.read_industry('twse')['rows']
        calls = []

        def down(url):
            calls.append(url)
            return 500, {}, b''
        seen = []
        with mock.patch.object(fetch_data, '_open', down):
            for k in (7, 8, 9, 10, 14, 15, 21, 22, 28, 29):
                n = len(calls)
                fetch_data.refresh_industry(D + dt.timedelta(days=k))
                seen.append((k, len(calls) - n))
        # 第 7 天失敗（等 1 天）、第 8 天失敗（等 2 天）、第 10 天失敗（等 4 天）、第 14 天失敗（等 7 天）、第 21 天失敗
        self.assertEqual([k for k, c in seen if c], [7, 8, 10, 14, 21, 28])
        j = fetch_data.read_industry('twse')
        self.assertEqual((j['stale'], j['fails'], j['fetched'], j['rows']), (True, 6, '2026-10-01', old))
        self.assertEqual(self.clock.sleeps, [])               # 不等待重試

    def test_structure_and_network_errors_only_warn(self):
        D = dt.date(2026, 10, 1)
        with mock.patch.object(fetch_data, '_open', return_value=(200, {}, b'[]')) as m:
            fetch_data.refresh_industry(D)
        self.assertEqual(m.call_count, 2)
        self.assertTrue(any('注意' in x and '上市' in x for x in self.industry_log()))
        self.assertTrue(fetch_data.read_industry('tpex')['stale'])
        with mock.patch.object(fetch_data, '_open', return_value=(200, {}, b'{"a":1}')):
            fetch_data.refresh_industry(D + dt.timedelta(days=30))
        with mock.patch.object(fetch_data, '_open', side_effect=OSError('x')) as m:
            fetch_data.refresh_industry(D + dt.timedelta(days=60))
        self.assertEqual(m.call_count, 1)                       # 網路不通：只送 1 次，不重試
        self.assertEqual(self.clock.sleeps, [])

    def test_parse_industry(self):
        p = fetch_data.parse_industry
        rows = [{'公司代號': '%04d' % (1100 + i), '產業別': '24'} for i in range(300)]
        rows += [{'SecuritiesCompanyCode': '6488', 'SecuritiesIndustryCode': '24'}, {'公司代號': '00631L', '產業別': '24'},
                 {'公司代號': '2330', '產業別': '<b>'}, {'公司代號': '0999', '產業別': '24'}, 'x', {'公司代號': 1234}]
        out = p(rows)
        self.assertEqual(len(out), 301)
        self.assertEqual(out['6488'], '24')
        self.assertNotIn('00631L', out)
        self.assertNotIn('2330', out)
        with self.assertRaises(fetch_data.Structure):
            p({'a': 1})
        with self.assertRaises(fetch_data.Structure):
            p(rows[:50])
        self.assertEqual(p([{'公司代號': '%04d' % (1100 + i), '產業別': '半導體業'} for i in range(300)])['1100'], '半導體業')

    def test_shares_limiter_with_tpex(self):
        with mock.patch.object(fetch_data, 'GAP', 3.0), mock.patch.object(fetch_data, '_open', return_value=(200, {}, b'{}')):
            fetch_data.get(fetch_data.TPEX_DAY.format(slash='2026/10/01'), lambda d: d)
            t = self.clock.t
            fetch_data.get(fetch_data.TPEX_IND, lambda d: d)
        self.assertEqual(self.clock.t - t, 3.0)

    def test_daily_exit_code_unaffected_and_once_a_day(self):
        TestFetch.preload(self)
        blocked = []

        def down(url):
            blocked.append(url)
            return 403, {}, b''
        with mock.patch.object(fetch_data, 'fetch_day', return_value='skipped'), \
                mock.patch.object(fetch_data, 'sync_exright'), mock.patch.object(fetch_data, '_open', down):
            self.assertEqual(fetch_data.cmd_daily(None), 0)
        self.assertTrue(any('t187ap03' in u for u in blocked))
        self.assertTrue(any('注意' in x for x in self.industry_log()))
        site = FakeSite()
        with mock.patch.object(fetch_data, 'fetch_day', return_value='skipped'), \
                mock.patch.object(fetch_data, 'sync_exright'), mock.patch.object(fetch_data, '_open', site):
            fetch_data._reset_net()
            self.assertEqual(fetch_data.cmd_daily(None), 0)          # 今天已經試過失敗 1 次：1 天後才再試，所以今天 0 次
            self.assertEqual([u for u in site.urls if 't187ap03' in u], [])

# ---------- K 線圖資料（data-ch） ----------
def rw_bars(n, seed, adjf=1.0, base=50.0, vol=500000, start=dt.date(2025, 6, 2)):
    """隨機漫步的日線；close 是還原價，raw_close = close × adjf（只有最後一根會被 analyze／chart_series 拿來還原）"""
    import random
    rnd = random.Random(seed)
    day, c, out = start, base, []
    while len(out) < n:
        if day.weekday() < 5:
            o = c * (1 + rnd.gauss(0, 0.01))
            c = max(0.01, o * (1 + rnd.gauss(0.001, 0.015)))
            out.append(dict(date=day.isoformat(), open=o, high=max(o, c) * (1 + abs(rnd.gauss(0, 0.006))),
                            low=min(o, c) * (1 - abs(rnd.gauss(0, 0.006))), close=c, volume=int(vol * (0.5 + rnd.random())),
                            raw_close=c * adjf))
        day += dt.timedelta(days=1)
    return out


def ref_chart_decode(s):
    """照規格寫的參考解碼器（獨立於 HTML_JS 的 chDec）"""
    assert re.fullmatch(r'[A-Za-z]+', s)
    p = [0]

    def u():
        v = 0
        while True:
            c = ord(s[p[0]])
            p[0] += 1
            if c < 91:
                v = v * 26 + c - 65
            else:
                return v * 26 + c - 97

    def z():
        v = u()
        return -(v + 1) // 2 if v % 2 else v // 2

    def grp(n):
        k = u()
        a = [None] * k
        if k < n:
            x = u()
            a.append(x)
            for _ in range(n - k - 1):
                x += z()
                a.append(x)
        return a

    def run(n):
        x = z()
        a = [x]
        for _ in range(n - 1):
            x += z()
            a.append(x)
        return a
    o = {}
    assert u() == 2
    n = o['n'] = u()
    o['e'] = u() - 2
    o['mo'], o['dy'] = u(), u()
    o['tk'] = [(u(), u()) for _ in range(u())]
    c = [u()]
    for _ in range(n - 1):
        c.append(c[-1] + z())
    op = [x + z() for x in c]
    h = [max(a, b) + u() for a, b in zip(op, c)]
    lo_ = [min(a, b) - u() for a, b in zip(op, c)]
    m20, m60, bw = grp(n), grp(n), grp(n)
    v = [u() for _ in range(n)]
    mv = grp(n)
    o['me'] = u() - 10
    dif, osc = run(n), run(n)
    b = u()
    o.update(c=c, o=op, h=h, l=lo_, m20=m20, m60=m60, bw=bw, v=v, mv=mv, dif=dif, osc=osc, wk=(b & 1, b >> 1 & 1, b >> 2 & 1, z(), z()))
    o['yr'], o['vk'], g = 2000 + u(), u(), []
    while len(g) < n - 1:
        t = u()
        g += [u(), u()] if t == 25 else [t // 5, t % 5]
    last = dt.date(o['yr'], o['mo'], o['dy'])
    off = [0]
    for x in reversed(g[:n - 1]):
        off.insert(0, off[0] + x + 1)
    o['dates'] = [(last - dt.timedelta(days=k)).isoformat() for k in off]
    assert p[0] == len(s)
    return o


class TestChart(unittest.TestCase):
    def test_t1_series_matches_analyze(self):
        import copy
        for n in (80, 119, 120, 121, 200, 260):
            for seed in range(6):
                for adjf in (1, 0.8):
                    bars = rw_bars(n, seed, adjf)
                    before = copy.deepcopy(bars)
                    r0 = screener.analyze('1234', 'x', bars)
                    s = screener.chart_series(bars)
                    r = screener.analyze('1234', 'x', bars)
                    tag = (n, seed, adjf)
                    self.assertEqual(bars, before, tag)
                    self.assertEqual(r, r0, tag)
                    self.assertEqual(len(s['c']), min(n, 120), tag)
                    self.assertEqual(round(s['c'][-1], 2), r['close'], tag)
                    for k, kk in (('ma20', 'ma20'), ('ma60', 'ma60'), ('up', 'boll_up'), ('lo', 'boll_lo')):
                        self.assertEqual(round(s[k][-1], 2), r['ma'][kk], (tag, k))
                    self.assertEqual([round(s['osc'][-2], 2), round(s['osc'][-1], 2)], r['osc'], tag)
                    self.assertEqual(round(s['wk'][0], 2), r['week']['dif'], tag)
                    self.assertEqual(round(s['wk'][1], 2), r['week']['osc'], tag)
                    self.assertEqual(s['date'][-1], bars[-1]['date'])

    def test_t2_roundtrip(self):
        import math
        for seed in range(8):
            for n, base in ((120, 50.0), (80, 800.0), (150, 3.5)):
                bars = rw_bars(n, seed, 0.9, base=base)
                s = screener.chart_series(bars)
                code = screener.chart_code(s)
                self.assertRegex(code, r'^[A-Za-z]{8,8000}$')
                d = ref_chart_decode(code)
                N = len(s['c'])
                self.assertEqual(d['n'], N)
                e = max(-2, math.floor(math.log10(max(s['h']))) - 3)
                step = 10.0 ** e
                q = (lambda x: round(x * 10 ** -e)) if e < 0 else (lambda x: round(x / 10 ** e))
                self.assertEqual(d['e'], e)
                self.assertEqual(d['c'], [q(x) for x in s['c']])
                self.assertEqual(d['o'], [q(x) for x in s['o']])
                self.assertEqual(d['h'], [q(x) for x in s['h']])
                self.assertEqual(d['l'], [q(x) for x in s['l']])
                for i in range(N):
                    self.assertGreaterEqual(d['h'][i], max(d['o'][i], d['c'][i]))
                    self.assertGreaterEqual(min(d['o'][i], d['c'][i]), d['l'][i])
                    for k, kk in (('c', 'c'), ('o', 'o'), ('h', 'h'), ('l', 'l')):
                        self.assertLessEqual(abs(d[kk][i] * step - s[k][i]), step / 2 + 1e-9)
                    for k, kk in (('ma20', 'm20'), ('ma60', 'm60')):
                        if s[k][i] is None:
                            self.assertIsNone(d[kk][i])
                        else:
                            self.assertLessEqual(abs(d[kk][i] * step - s[k][i]), step / 2 + 1e-9)
                    if s['up'][i] is not None:
                        self.assertLessEqual(abs((d['m20'][i] + d['bw'][i]) * step - s['up'][i]), step + 1e-9)
                        self.assertLessEqual(abs((d['m20'][i] - d['bw'][i]) * step - s['lo'][i]), step + 1e-9)
                    ms = 10.0 ** d['me']
                    self.assertLessEqual(abs(d['dif'][i] * ms - s['dif'][i]), ms / 2 + 1e-9)
                    if s['osc'][i] != 0:
                        self.assertEqual(d['osc'][i] > 0, s['osc'][i] > 0, (seed, n, i))
                        self.assertNotEqual(d['osc'][i], 0)
                    self.assertTrue(0 <= d['v'][i] <= 675)
                self.assertEqual((d['mo'], d['dy']), (int(s['date'][-1][5:7]), int(s['date'][-1][8:10])))
                self.assertEqual(d['wk'][3], round(s['wk'][0] * 100))
                self.assertEqual(d['dates'], s['date'])
                self.assertEqual(d['vk'], round(max(s['v']) / 1000))
                self.assertEqual(bool(d['wk'][2]), bool(s['wk'][2]))

    def test_t3_edges(self):
        import math
        d = ref_chart_decode(screener.chart_code(screener.chart_series(rw_bars(80, 1))))
        self.assertEqual(d['n'], 80)
        self.assertEqual(d['m60'].count(None), 59)
        self.assertEqual(d['m20'].count(None), 19)
        self.assertEqual(d['bw'].count(None), 19)
        self.assertEqual(d['mv'].count(None), 4)
        flat = [dict(date=b['date'], open=10.0, high=10.0, low=10.0, close=10.0, volume=1000, raw_close=10.0) for b in rw_bars(100, 1)]
        d = ref_chart_decode(screener.chart_code(screener.chart_series(flat)))
        self.assertEqual(d['me'], -10)
        self.assertEqual(set(d['osc']), {0})
        for base in (0.5, 25000.0):
            s = screener.chart_series(rw_bars(90, 2, 1, base=base))
            d = ref_chart_decode(screener.chart_code(s))
            self.assertEqual(d['e'], max(-2, math.floor(math.log10(max(s['h']))) - 3))
            self.assertEqual(len(s['c']), 90)
        bars = rw_bars(100, 3)
        bars[50]['close'] = float('nan')
        with self.assertRaises(ValueError):
            screener.chart_code(screener.chart_series(bars))
        s = screener.chart_series(rw_bars(100, 3))
        s['l'][3] = 0.0
        with self.assertRaises(ValueError):
            screener.chart_code(s)
        s = screener.chart_series(rw_bars(100, 3))
        with mock.patch.object(screener, 'CHART_MAX', 100):
            with self.assertRaises(ValueError):
                screener.chart_code(s)

    def _codes(self):
        return {'1101': screener.chart_code(screener.chart_series(rw_bars(150, 1))),
                '2454': screener.chart_code(screener.chart_series(rw_bars(150, 2)))}

    def test_t4_render_html(self):
        res = fake_results()
        codes = self._codes()
        h0 = screener.render_html(res, False, meta_for())
        h = screener.render_html(res, False, meta_for(charts=codes))
        self.assertEqual(h0, screener.render_html(res, False, meta_for(charts={})))
        self.assertEqual(h.count(' data-ch="'), 2)
        self.assertIn('data-mkt="" data-ch="%s">' % codes['1101'], h)
        self.assertRegex(h, r'<tbody class="cand" [^>]*data-code="1101"[^>]* data-ch="[A-Za-z]+"><tr class="r1">')
        self.assertRegex(h, r'<tr data-i="0" data-code="2454"[^>]* data-ch="[A-Za-z]+"><th')
        self.assertEqual(h.count('<script'), 3)
        self.assertEqual(h0.count('<script'), 3)
        self.assertEqual(re.sub(r' data-ch="[A-Za-z]+"', '', h), h0)
        self.assertNotIn('<svg', re.sub(r'<script>.*?</script>', '', h, flags=re.S))      # 靜態 HTML 沒有 svg，只有程式碼裡的字串

    def test_t5_scan(self):
        data = os.path.join(self._td_path(), 'data')
        make_data(data, ('1101', '2330', '2454'))
        real = screener.analyze

        def fake(code, name, bars):
            r = real(code, name, bars)
            if code == '2330':
                r.update(type='回測型', stop=round(r['close'] * 0.95, 2), setup='測試', stop_why='測試', watch=True)
            elif code == '2454':
                r.update(watch=True)
                r.pop('type', None)
            else:
                r.update(watch=False)
                r.pop('type', None)
            return r

        def run(tag, raiser=False):
            out = os.path.join(self._td_path(), tag)
            a = argparse.Namespace(data=data, config=None, list=None, capital=1000000, risk=None, out=None, json=None, txt=None,
                                   html=None, csv=None, out_dir=out, open=False, share=False)
            with mock.patch.object(screener, 'analyze', fake):
                if raiser:
                    with mock.patch.object(screener, 'chart_code', side_effect=ValueError('x')):
                        quiet(screener.cmd_scan, a)
                else:
                    quiet(screener.cmd_scan, a)
            return {e: readb(os.path.join(out, [n for n in os.listdir(out) if n.endswith('.' + e)][0])) for e in ('html', 'md', 'txt', 'json', 'csv')}
        a, b = run('a'), run('b', True)
        ha = a['html'].decode('utf-8')
        self.assertEqual(ha.count(' data-ch="'), 2)
        self.assertRegex(ha, r'data-code="2330"[^>]* data-ch="')
        self.assertRegex(ha, r'data-code="2454"[^>]* data-ch="')
        self.assertNotIn(' data-ch="', b['html'].decode('utf-8'))
        for e in ('md', 'txt', 'json', 'csv'):
            self.assertEqual(a[e], b[e], e)

    def _td_path(self):
        if not hasattr(self, '_tdo'):
            self._tdo = tempfile.TemporaryDirectory(prefix='線圖 (x86) ')
            self.addCleanup(self._tdo.cleanup)
        return self._tdo.name

    def test_t6_js_static(self):
        js = screener.HTML_JS
        for bad in ('=>', '`', 'createElementNS', 'xmlns', 'http', 'src=', '@', 'data:'):
            self.assertNotIn(bad, js, bad)
        self.assertNotRegex(js, r'(?m)^[^/\n]*[^:\s/][ \t]*//')       # 沒有行尾註解
        self.assertNotRegex(js, r'\b(?:let|const|class)\s')
        sj = screener.SHARE_JS
        self.assertIn('chDec', sj)
        self.assertIn('Escape', sj)
        self.assertNotIn('wt', sj)
        self.assertNotRegex(sj, r'[0-9]{5}')
        self.assertNotRegex(sj, r'[0-9],[0-9]{3}')
        self.assertNotRegex(sj, r'(?<![A-Za-z0-9_-])lh(?![A-Za-z0-9_-])')
        self.assertNotRegex(sj, r'[0-9]e\+?[0-9]')
        for k in screener.CONFIG_KEYS:
            self.assertNotRegex(sj, r'(?<![A-Za-z0-9_-])' + k + r'\s*=')
        self.assertNotIn('<!--', sj)
        self.assertNotIn('</script', sj)
        for w in ('部位', '總資金', '風險', '自選股', '觀察名單', '試算'):
            self.assertNotIn(w, sj)
        self.assertNotRegex(screener.HTML_CSS, r'(?<![A-Za-z-])lh(?![A-Za-z-])|data:|url\(|transition|animation')

    def test_t7_chart_colors_contrast(self):
        css = screener.HTML_CSS
        toks = set()
        for m in re.finditer(r'\.cx-[a-z0-9]+(?:,\.cx-[a-z0-9]+)*\{([^}]*)\}', css):
            toks |= set(re.findall(r'(?:fill|stroke):var\(--([a-z0-9-]+)\)', m.group(1)))
        toks -= {'hairline', 'card'}
        self.assertTrue({'up', 'down', 'ink', 'ink2', 'link', 'cap', 'warn'} <= toks, toks)
        for name in ('light', 'dark'):
            p = screener.PALETTE[name]
            for t in toks:
                for bg in ('page', 'surface'):
                    self.assertGreaterEqual(wcag_ratio(p[t.replace('-', '_')], p[bg]), 4.5, (name, t, bg))

    def test_t8_length(self):
        lens = []
        for seed in range(20):
            lens.append(len(screener.chart_code(screener.chart_series(rw_bars(120, seed, 0.95, base=20 + seed * 15)))))
        avg = sum(lens) / len(lens)
        print('\n[T8] data-ch 平均長度 %.0f（最大 %d）' % (avg, max(lens)))
        self.assertLessEqual(avg, 2600)
        self.assertLess(max(lens), screener.CHART_MAX)


def ref_kd(h, l, c, w=0):
    """KD(9,3,3) 參考算法（和頁面的 chKD 同一個定義）"""
    K, D, k, q = [], [], 50.0, 50.0
    for i in range(len(c)):
        if i < 8:
            K.append(None)
            D.append(None)
            continue
        hh, ll = max(h[i - 8:i + 1]), min(l[i - 8:i + 1])
        rv = (c[i] - ll) / (hh - ll) * 100 if hh > ll else 50
        k = k * 2 / 3 + rv / 3
        q = q * 2 / 3 + k / 3
        K.append(None if i < w else k)
        D.append(None if i < w else q)
    return K, D


def ref_rsi(c, n, w=0):
    """RSI（Wilder）參考算法"""
    r, g, s = [], 0.0, 0.0
    for i in range(len(c)):
        x = c[i] - c[i - 1] if i else 0
        if i <= n:
            g, s = g + max(x, 0), s + max(-x, 0)
            if i == n:
                g, s = g / n, s / n
        else:
            g, s = (g * (n - 1) + max(x, 0)) / n, (s * (n - 1) + max(-x, 0)) / n
        r.append(None if i < n or i < w else (g / (g + s) * 100 if g + s > 0 else 50))
    return r


def round_js(x):
    import math
    return math.floor(x + 0.5)


def ref_win(n, a, m):
    m = max(min(n, 20), min(n, round_js(m)))
    return [max(0, min(n - m, round_js(a))), m]


def ref_zoom(n, a, m, f, t):
    m2 = ref_win(n, 0, m * f)[1]
    return ref_win(n, a + t * (m - m2), m2)


def js_funcs(*names):
    out = []
    for nm in names:
        m = re.search(r'\n  function %s\(.*?\n  }\n' % nm, screener.HTML_JS, re.S)
        assert m, nm
        out.append(m.group(0))
    return ''.join(out)


NODE = shutil.which('node')


def run_node(src, payload):
    p = subprocess.run([NODE, '-e', src], input=json.dumps(payload), capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


NODE_MAIN = """
var s = '';
process.stdin.on('data', function (x) { s += x; });
process.stdin.on('end', function () { var a = JSON.parse(s); process.stdout.write(JSON.stringify(a.map(go))); });
"""


class TestChartIndicators(unittest.TestCase):
    """頁面上的指標、縮放（JS）和 Python 參考算法比對；沒有 node 就跳過要跑 JS 的部分"""

    def close(self, a, b, tol, tag):
        self.assertEqual(len(a), len(b), tag)
        for i, (x, y) in enumerate(zip(a, b)):
            if x is None or y is None:
                self.assertEqual(x, y, (tag, i))
            else:
                self.assertLessEqual(abs(x - y), tol, (tag, i, x, y))

    @unittest.skipUnless(NODE, 'node not installed')
    def test_i1_js_matches_reference(self):
        src = js_funcs('chSma', 'chKD', 'chRSI') + """
function go(q) { var kd = chKD(q.h, q.l, q.c, q.w); return [chSma(q.c, 5), kd[0], kd[1], chRSI(q.c, 5, q.w), chRSI(q.c, 10, q.w)]; }
""" + NODE_MAIN
        import random
        cases = []
        for seed in range(12):
            d = ref_chart_decode(screener.chart_code(screener.chart_series(rw_bars(150, seed, 0.9, base=5 + seed * 40))))
            cases.append(dict(h=d['h'], l=d['l'], c=d['c'], w=seed % 3 * 11))
        flat = [100] * 40
        cases.append(dict(h=flat, l=flat, c=flat, w=0))
        rnd = random.Random(5)
        c = [rnd.randint(90, 110) for _ in range(60)]
        cases.append(dict(h=[x + rnd.randint(0, 3) for x in c], l=[x - rnd.randint(0, 3) for x in c], c=c, w=0))
        got = run_node(src, cases)
        for k, (q, g) in enumerate(zip(cases, got)):
            K, D = ref_kd(q['h'], q['l'], q['c'], q['w'])
            sm = [None if i < 4 else sum(q['c'][i - 4:i + 1]) / 5 for i in range(len(q['c']))]
            for tag, a, b in (('sma', g[0], sm), ('k', g[1], K), ('d', g[2], D), ('r5', g[3], ref_rsi(q['c'], 5, q['w'])),
                              ('r10', g[4], ref_rsi(q['c'], 10, q['w']))):
                self.close(a, b, 1e-9, (k, tag))
        self.assertEqual(set(x for x in got[-2][1] if x is not None), {50})

    @unittest.skipUnless(NODE, 'node not installed')
    def test_i2_approximation_vs_full_history(self):
        """頁面只有 120 根、價格 4 位有效數：和用完整歷史、不量化算出來的值比，畫出來的部分誤差要小"""
        src = js_funcs('chSma', 'chKD', 'chRSI', 'chDec') + """
function go(s) { var o = chDec(s); return [o.k, o.kd, o.r5, o.r10, o.n, o.vk]; }
""" + NODE_MAIN
        codes, full = [], []
        for seed in range(16):
            bars = rw_bars(260, seed, 0.85, base=8 + seed * 37)
            codes.append(screener.chart_code(screener.chart_series(bars)))
            adj = bars[-1]['raw_close'] / bars[-1]['close']
            full.append([[b[k] * adj for b in bars] for k in ('high', 'low', 'close')])
        got = run_node(src, codes)
        worst, tot, cnt = [0.0] * 4, [0.0] * 4, [0] * 4
        for (h, l, c), g in zip(full, got):
            n = g[4]
            K, D = ref_kd(h, l, c)
            ref = [K[-n:], D[-n:], ref_rsi(c, 5)[-n:], ref_rsi(c, 10)[-n:]]
            for j in range(4):
                for x, y in zip(g[j], ref[j]):
                    if x is not None:
                        worst[j] = max(worst[j], abs(x - y))
                        tot[j] += abs(x - y)
                        cnt[j] += 1
            self.assertEqual([x is None for x in g[0]], [i < 22 for i in range(n)])
            self.assertEqual([x is None for x in g[3]], [i < 40 for i in range(n)])
        mean = [t / k for t, k in zip(tot, cnt)]
        print('\n[I2] 最大誤差 K %.3f D %.3f RSI5 %.3f RSI10 %.3f；平均 %.3f %.3f %.3f %.3f' % tuple(worst + mean))
        self.assertLess(max(worst), 2.5, worst)
        self.assertLess(max(mean), 0.3, mean)

    @unittest.skipUnless(NODE, 'node not installed')
    def test_i3_zoom_window(self):
        src = js_funcs('chWin', 'chZoom') + """
function go(q) { return q.length === 3 ? chWin(q[0], q[1], q[2]) : chZoom(q[0], q[1], q[2], q[3], q[4]); }
""" + NODE_MAIN
        import random
        rnd = random.Random(1)
        cases = []
        for _ in range(400):
            n = rnd.choice((2, 15, 20, 21, 80, 119, 120))
            cases.append([n, rnd.uniform(-30, 150), rnd.uniform(-5, 200)])
            w = ref_win(n, rnd.uniform(0, n), rnd.uniform(1, n))
            cases.append([n, w[0], w[1], rnd.choice((1 / 1.5, 1.5, 1.2, 1 / 1.2, 0.1, 9)), rnd.random()])
        got = run_node(src, cases)
        for q, g in zip(cases, got):
            want = ref_win(*q) if len(q) == 3 else ref_zoom(*q)
            self.assertEqual(g, want, q)
            n, (a, m) = q[0], g
            self.assertTrue(min(n, 20) <= m <= n and 0 <= a <= n - m, (q, g))

    def test_i4_zoom_properties(self):
        self.assertEqual(ref_win(120, 0, 120), [0, 120])
        self.assertEqual(ref_win(120, 60, 60), [60, 60])
        self.assertEqual(ref_win(120, 0, 5), [0, 20])
        self.assertEqual(ref_win(12, 3, 5), [0, 12])
        self.assertEqual(ref_win(120, 200, 30), [90, 30])
        a, m = 0, 120
        for _ in range(10):
            a, m = ref_zoom(120, a, m, 1 / 1.5, 1)
            self.assertEqual(a + m, 120)
        self.assertEqual(m, 20)
        a, m = ref_zoom(120, 40, 40, 1 / 1.5, 0.5)
        self.assertLessEqual(abs((a + m / 2) - 60), 1)

    def test_i5_js_static(self):
        js, sj, css = screener.HTML_JS, screener.SHARE_JS, screener.HTML_CSS
        for line in js.split('\n'):
            if 'localStorage' in line:
                self.assertIn('try', line, line)
        for k in ('twss-ind', 'chKD', 'chRSI', 'chZoom', 'pointerdown', 'pointercancel', "'wheel'", 'passive: false', 'aria-pressed',
                  'aria-live', 'ctrlKey', 'ArrowLeft', 'setPointerCapture', '前一天'):
            self.assertIn(k, sj + screener.PWA_JS, k)
        m = re.search(r"var chOn = (\{[^}]*\})", js)
        self.assertEqual(set(re.findall(r'(\w+): true', m.group(1))), {'ma20', 'ma60', 'bb', 'vol', 'macd'})
        self.assertRegex(css, r'\.chq\{touch-action:pan-y')
        self.assertIn('.chp .chip,.chx{min-height:44px;min-width:44px}', css)
        self.assertLess(js.index('function chEvents'), js.index('\n  // 觀察名單\n'))
        page = screener.render_html(fake_results(), False, meta_for(charts={'1101': screener.chart_code(screener.chart_series(rw_bars(150, 1)))}))
        self.assertEqual(page.count('<script'), 3)


if __name__ == '__main__':
    unittest.main()
