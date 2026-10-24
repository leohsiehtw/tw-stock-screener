"""易用性測試：設定檔、自選股清單、HTML／CSV 輸出、錯誤代碼、.bat 檔格式。只用 unittest，不連網。

執行：python -m unittest discover -s tests -v
"""
import argparse, contextlib, csv, datetime as dt, io, json, os, re, subprocess, sys, tempfile, threading, unittest
import urllib.error
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


class TestRender(unittest.TestCase):
    def test_html_content(self):
        res = fake_results()
        h = screener.render_html(res, False, meta_for())
        self.assertIn('lang="zh-Hant"', h)
        self.assertIn('charset="utf-8"', h)
        self.assertIn('元大S&amp;P500', h)
        self.assertNotIn('<測試>', h)
        self.assertNotIn('<b>x</b>', h)
        for bad in ('http://', 'https://', '<link', 'src=', '@import', 'url('):
            self.assertNotIn(bad, h)
        for cls in ('sortable', 't-break', 't-pull', 'p-cap', 'p-odd', 'd-wide', 'd-ok'):
            self.assertIn(cls, h)
        self.assertIn('2026-10-22（星期四）', h)
        self.assertIn('掃描 7 檔（資料有效 6 檔）', h)
        self.assertIn('部位（風險 2%）', h)
        self.assertIn('部位（5%）', h)
        self.assertIn('觀察名單', h)
        self.assertNotIn('資料日期不是今天', h)
        self.assertIn('資料只有 10 天', h)

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
        before = json.dumps(res, ensure_ascii=False, default=str)
        screener.render_html(res, True, meta_for())
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
        self.assertEqual(rows[0][-3:], ['說明', '停損依據', '備註'])
        self.assertTrue(all(len(r) == len(rows[0]) for r in rows))
        codes = [r[0] for r in rows[1:]]
        self.assertIn('="0050"', codes)
        self.assertEqual(codes[-1], '="9999"')              # 錯誤列排最後
        self.assertIn('資料只有 10 天', rows[-1][-1])
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


class TestFetch(TempCase):
    def setUp(self):
        super().setUp()
        for target, val in (('GAP', 0), ('RETRY', ()), ('CACHE', os.path.join(self.tmp, 'cache')),
                            ('DATA', os.path.join(self.tmp, 'data'))):
            p = mock.patch.object(fetch_data, target, val)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(fetch_data.time, 'sleep', side_effect=AssertionError('不該等待'))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(fetch_data, 'log')
        p.start()
        self.addCleanup(p.stop)

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
        with mock.patch.object(fetch_data.urllib.request, 'urlopen', side_effect=urllib.error.URLError('x')):
            with self.assertRaises(fetch_data.FetchError) as cm:
                fetch_data.http_json('https://example.invalid/')
        self.assertEqual(cm.exception.kind, 'network')
        err = urllib.error.HTTPError('https://example.invalid/', 403, 'forbidden', {}, None)
        with mock.patch.object(fetch_data.urllib.request, 'urlopen', side_effect=err):
            with self.assertRaises(fetch_data.FetchError) as cm:
                fetch_data.http_json('https://example.invalid/')
        self.assertEqual(cm.exception.kind, 'site')

    def test_daily_network_down(self):
        self.preload()
        with mock.patch.object(fetch_data.urllib.request, 'urlopen', side_effect=urllib.error.URLError('x')) as m:
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
        with mock.patch.object(fetch_data.urllib.request, 'urlopen', side_effect=urllib.error.URLError('x')) as m:
            with self.assertRaises(fetch_data.UserError) as cm:
                fetch_data.cmd_backfill(a)
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual(m.call_count, 1)

    def test_build_empty_cache(self):
        with self.assertRaises(fetch_data.UserError) as cm:
            fetch_data.build()
        self.assertEqual(cm.exception.code, 6)


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


if __name__ == '__main__':
    unittest.main()
