// Integration checks: every feature stays reachable from the app shell, and the
// Settings library behaves. The api module is mocked, so no test reaches a backend.
import { render, screen, within, fireEvent, waitFor, act } from '@testing-library/react';
import App from './App';
import { accountsApi, tradesApi, libraryApi, kpisApi, goalsApi, mt5TimezoneApi, chartApi, attachmentsApi, backupApi, __restoreMocks } from './api';
import TradeDetail from './components/TradeDetail';
import { clipboardFiles } from './useFilePaste';

jest.mock('./api', () => {
  const ok = (data) => Promise.resolve({ data });
  // CRA's jest preset resets mocks before each test, so every mock keeps its
  // implementation and __restoreMocks puts it back in beforeEach.
  const all = [];
  const backupState = { folder: '', set: false, exists: false };
  const fn = (impl) => { const f = jest.fn(impl); f.impl = impl; all.push(f); return f; };
  const TRADE = {
    id: 101, account_id: 1, trade_group: '9/10/26_TSLA_STOCK_1', date: '2026-09-10', ticker: 'TSLA',
    instrument_type: 'STOCK', side: 'LONG', gross_pnl: 195, net_pnl: 193.45, commissions: 1.55,
    executions: [
      { date: '2026-09-10', time: '09:54:10', action: 'BOT', qty: 200, price: 366.09, commission: 0 },
      { date: '2026-09-10', time: '10:08:24', action: 'SOLD', qty: 200, price: 367.07, commission: 1.55 },
    ],
    setup: 'VWAP Reclaim', setup_grade: 'B', setup_notes: null, setup_source: 'manual',
    mfe_pct: 0.85, mae_pct: -0.27, exit_efficiency: 31.25, strategy: 'Test Strategy', r_multiple: 0.41,
  };
  const TRADE_2 = { ...TRADE, id: 102, trade_group: '9/10/26_META_STOCK_1', ticker: 'META', net_pnl: -50 };
  // 300 bought, 100 sold: 200 still open.
  const OPEN_TRADE = {
    id: 103, account_id: 1, trade_group: '9/09/26_GOOG_STOCK_1', date: '2026-09-09', ticker: 'GOOG',
    instrument_type: 'STOCK', side: 'LONG', net_pnl: null, gross_pnl: null, commissions: 0,
    executions: [
      { date: '2026-09-09', time: '09:40:00', action: 'BOT', qty: 300, price: 100, commission: 0 },
      { date: '2026-09-09', time: '11:00:00', action: 'SOLD', qty: 100, price: 105, commission: 0 },
    ],
  };
  const ACCOUNTS = [
    { id: 1, name: 'Day Trading', type: 'day_trading', color: '#6366f1', broker: 'Schwab' },
    { id: 2, name: 'Swing', type: 'swing', color: '#6366f1', broker: 'Schwab' },
  ];
  const MT5_TIMEZONE = {
    timezone: '', effective: 'Europe/Athens', source: 'env', valid: true, error: null,
    current_offset_hours: 3, candidates: ['Europe/Athens', 'Europe/London'],
    transitions: [
      { at_utc: '2026-03-29 01:00:00', offset_hours_before: 2, offset_hours_after: 3 },
    ],
  };
  const LIBRARY = {
    strategies: [
      { name: 'VWAP Cross', description: 'Reclaim of VWAP', trades: 10, aliases: [], },
      { name: 'Continuation RS', description: null, trades: 2, aliases: [], },
    ],
    sources: [
      { name: 'Scanner', description: null, trades: 8, aliases: [] },
      { name: 'OneOption', description: null, trades: 1, aliases: [] },
    ],
    tags: {
      mistake: [{ name: 'Sized too big', description: null, trades: 6, aliases: [] }],
      execution: [{ name: 'Scaled out', description: null, trades: 20, aliases: [] }],
      setup: [], emotion: [], outcome: [],
    },
    tag_types: ['setup', 'execution', 'mistake', 'emotion', 'outcome'],
  };
  // Anything not listed resolves with an empty object, which every page treats as "no data".
  const withDefault = (methods) => new Proxy(methods, {
    get: (target, key) => (key in target ? target[key] : fn(() => ok({}))),
  });
  return {
    // Each test starts with no backup folder chosen; without this the folder
    // saved by one test would leak into the next and leave Backup enabled.
    __restoreMocks: () => {
      Object.assign(backupState, { folder: '', set: false, exists: false });
      all.forEach(f => f.mockImplementation(f.impl));
    },
    API_BASE: 'http://mocked.invalid',
    accountsApi: withDefault({
      list: fn(() => ok(ACCOUNTS)),
      create: fn(() => ok({ id: 3 })),
      update: fn(() => ok({})),
    }),
    tradesApi: withDefault({
      list: fn((params = {}) => ok(params.open_only ? [OPEN_TRADE] : [TRADE, TRADE_2])),
      addExecution: fn(() => ok({})),
      getAnalysis: fn(() => ok(null)),
      getAnalysisOptions: fn(() => ok({ strategies: [], idea_sources: [] })),
      listCustomSetups: fn(() => ok([])),
    }),
    kpisApi: withDefault({ get: fn(() => ok({ total_net_pnl: 100, daily_pnl: [], by_strategy: [] })) }),
    importApi: withDefault({}),
    diaryApi: withDefault({ list: fn(() => ok([])) }),
    chartApi: withDefault({ get: fn(() => ok({ bars: [], warning: 'No chart data in tests' })) }),
    insightsApi: withDefault({}),
    calendarApi: withDefault({ get: fn(() => ok({ days: [] })) }),
    brainApi: withDefault({}),
    dailySummaryApi: withDefault({ get: fn(() => ok({ trades: [] })) }),
    syncApi: withDefault({}),
    goalsApi: withDefault({ get: fn(() => ok({ win_rate: 65 })), put: fn(() => ok({ win_rate: 65 })) }),
    reportsApi: withDefault({ get: fn(() => Promise.reject(new Error('no reports in tests'))) }),
    edgeReportApi: withDefault({}),
    weeklySummaryApi: withDefault({}),
    yearlyKpisApi: withDefault({ get: fn(() => ok({ months: [] })) }),
    libraryApi: withDefault({
      list: fn(() => ok(LIBRARY)),
      merge: fn(() => ok({ moved: 2, target: 'VWAP Cross', trades: 12 })),
      remove: fn(() => ok({ affected: 1, reassigned_to: null })),
      update: fn(() => ok({ name: 'VWAP Cross', trades: 10 })),
      create: fn(() => ok({ name: 'New' })),
    }),
    mt5TimezoneApi: withDefault({
      get: fn(() => ok(MT5_TIMEZONE)),
      put: fn(() => ok({ ...MT5_TIMEZONE, timezone: 'Europe/London', effective: 'Europe/London', source: 'setting', valid: true })),
    }),
    backupApi: withDefault({
      // Stateful: saving a folder then re-reading it is the real sequence, so a
      // stub that always answered "" would un-disable the button again.
      getDestination: fn(() => ok(backupState)),
      setDestination: fn((folder) => {
        Object.assign(backupState, { folder, set: true, exists: true });
        return ok({ ...backupState });
      }),
      list: fn(() => ok({ folder: backupState.folder, archives: [] })),
      create: fn(() => ok({ name: 'tdjournal-backup.zip', size_bytes: 1024, folder: backupState.folder })),
      restore: fn(name => ok({ folder: '/tmp/restored', name, restored_members: 4, manifest: {} })),
      exportUrl: fn(fmt => `http://mocked.invalid/api/export?fmt=${fmt}`),
    }),
    attachmentsApi: withDefault({
      list: fn(() => ok({ attachments: [] })),
      upload: fn(() => ok({})),
      remove: fn(() => ok({})),
      downloadUrl: fn(id => `http://mocked.invalid/api/attachments/${id}/download`),
      previewUrl: fn(id => `http://mocked.invalid/api/attachments/${id}/download?inline=true`),
    }),
  };
});

beforeAll(() => {
  // jsdom gaps that charts and menus touch.
  global.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
  Element.prototype.scrollIntoView = jest.fn();
  window.alert = jest.fn();
});

beforeEach(() => __restoreMocks());

async function renderApp() {
  render(<App />);
  // Accounts load on mount; waiting for them lets the first render settle.
  await waitFor(() => expect(accountsApi.list).toHaveBeenCalled());
  await act(async () => {});
}

const nav = () => screen.getByRole('navigation', { name: 'Main' });

// ── Attachments ────────────────────────────────────────────────────────────
// The window-level paste listener and the Files tab are the two things that
// only exist together: a screenshot copied elsewhere in the app has to land on
// the open trade, not be swallowed because the user was looking at Stats.
describe('trade attachments', () => {
  const TRADE = {
    id: 101, account_id: 1, trade_group: '9/10/26_TSLA_STOCK_1', date: '2026-09-10', ticker: 'TSLA',
    instrument_type: 'STOCK', side: 'LONG', gross_pnl: 195, net_pnl: 193.45, commissions: 1.55,
    executions: [
      { date: '2026-09-10', time: '09:54:10', action: 'BOT', qty: 200, price: 366.09, commission: 0 },
      { date: '2026-09-10', time: '10:08:24', action: 'SOLD', qty: 200, price: 367.07, commission: 1.55 },
    ],
  };

  const renderTrade = async () => {
    render(<TradeDetail trade={TRADE} />);
    await waitFor(() => expect(attachmentsApi.list).toHaveBeenCalledWith('9/10/26_TSLA_STOCK_1'));
    await act(async () => {});
  };

  const pasteFiles = (clipboardData, target = document.body) => {
    // jsdom's ClipboardEvent is inconsistent across versions; mimic the native
    // DataTransfer properties the production helper reads.
    const event = new Event('paste', { bubbles: true, cancelable: true });
    Object.defineProperty(event, 'clipboardData', { value: clipboardData });
    act(() => { target.dispatchEvent(event); });
    return event;
  };

  test('extracts Lightshot-style image files exposed only through clipboard items', () => {
    const image = new File(['png bytes'], 'lightshot.png', { type: 'image/png' });
    const clipboardData = {
      files: [],
      items: [{ kind: 'file', type: 'image/png', getAsFile: () => image }],
    };
    expect(clipboardFiles(clipboardData)).toEqual([image]);
  });

  test('deduplicates the same image exposed through files and items', () => {
    const image = new File(['png bytes'], 'lightshot.png', { type: 'image/png' });
    const clipboardData = {
      files: [image],
      items: [{ kind: 'file', type: 'image/png', getAsFile: () => image }],
    };
    expect(clipboardFiles(clipboardData)).toEqual([image]);
  });

  test('the Files tab is offered and lists nothing until something is attached', async () => {
    await renderTrade();
    const tabs = screen.getAllByRole('tab').map(t => t.textContent);
    expect(tabs).toEqual(['Stats', 'Strategy', 'Tags', 'Executions', 'Files', 'What If']);
    // The list request fires even while Stats is showing: the panel stays
    // mounted so a paste never has to mount it first.
    expect(attachmentsApi.list).toHaveBeenCalledWith('9/10/26_TSLA_STOCK_1');

    fireEvent.click(screen.getByRole('tab', { name: 'Files' }));
    expect(await screen.findByText(/No attachments yet/)).toBeInTheDocument();
  });

  test('a pasted image is uploaded once even when a pending-file effect reruns', async () => {
    attachmentsApi.upload.mockImplementation(() => Promise.resolve({
      data: {
        id: 7, trade_group: '9/10/26_TSLA_STOCK_1', original_name: 'pasted-image.png',
        stored_name: 'abc.png', content_type: 'image/png', size_bytes: 12, kind: 'image',
      },
    }));
    await renderTrade();

    // Lightshot-style: the image is only on clipboardData.items.
    const image = new File(['x'], '', { type: 'image/png' });
    pasteFiles({ files: [], items: [{ kind: 'file', type: 'image/png', getAsFile: () => image }] });
    await screen.findByText('pasted-image.png');
    // Wait a beat for state updates after completion; the same array/callback
    // combination must not cause the panel effect to submit the file again.
    await act(async () => {});
    expect(attachmentsApi.upload).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('tab', { name: 'Files' })).toHaveAttribute('aria-selected', 'true');
  });

  test('a pasted image is sent with a name the backend allowlist can validate', async () => {
    attachmentsApi.upload.mockImplementation(() => Promise.resolve({
      data: {
        id: 7, trade_group: '9/10/26_TSLA_STOCK_1', original_name: 'pasted-image.png',
        stored_name: 'abc.png', content_type: 'image/png', size_bytes: 12, kind: 'image',
      },
    }));
    await renderTrade();

    // Pasted from the Stats tab — the tab has to follow the file.
    pasteFiles({ files: [new File(['x'], '', { type: 'image/png' })], items: [] });

    await waitFor(() => expect(attachmentsApi.upload).toHaveBeenCalledTimes(1));
    expect(screen.getByRole('tab', { name: 'Files' })).toHaveAttribute('aria-selected', 'true');

    const form = attachmentsApi.upload.mock.calls[0][1];
    // A clipboard blob has no name; the panel supplies one so the backend
    // allowlist has an extension to validate.
    expect(form.get('file').name).toBe('pasted-image.png');
    expect(await screen.findByText('pasted-image.png')).toBeInTheDocument();
  });

  test('an unsupported file is refused before it is sent', async () => {
    await renderTrade();
    pasteFiles({ files: [new File(['MZ'], 'payload.exe', { type: 'application/x-msdownload' })], items: [] });

    await screen.findByRole('alert');
    expect(screen.getByRole('alert')).toHaveTextContent(/can’t be attached/);
    expect(attachmentsApi.upload).not.toHaveBeenCalled();
  });

  test('a file over 50 MB is refused before it is sent', async () => {
    await renderTrade();
    const huge = { name: 'huge.png', size: 50 * 1024 * 1024 + 1, type: 'image/png' };
    pasteFiles({ files: [huge], items: [] });

    await screen.findByRole('alert');
    expect(screen.getByRole('alert')).toHaveTextContent(/over the 50 MB/);
    expect(attachmentsApi.upload).not.toHaveBeenCalled();
  });

  test('preview opens in an expandable popup; unsupported formats only download', async () => {
    attachmentsApi.list.mockImplementation(() => Promise.resolve({ data: { attachments: [
      { id: 1, original_name: 'chart.png', kind: 'image', content_type: 'image/png', size_bytes: 128, previewable: true },
      { id: 2, original_name: 'statement.pdf', kind: 'file', content_type: 'application/pdf', size_bytes: 2048, previewable: true },
      { id: 3, original_name: 'fills.csv', kind: 'file', content_type: 'text/csv', size_bytes: 64, previewable: true },
      { id: 4, original_name: 'journal.xlsx', kind: 'file', content_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', size_bytes: 512, previewable: false },
    ] } }));
    const body = 'date,ticker,pnl\n2026-09-10,TSLA,195';
    global.fetch = jest.fn(() => Promise.resolve(
      new Response(body, { status: 200, headers: { 'Content-Type': 'text/csv' } }),
    ));
    await renderTrade();
    fireEvent.click(screen.getByRole('tab', { name: 'Files' }));

    fireEvent.click(screen.getByRole('button', { name: 'Preview chart.png' }));
    const dialog = await screen.findByRole('dialog', { name: 'chart.png' });
    expect(await within(dialog).findByRole('img', { name: 'Preview of chart.png' })).toBeInTheDocument();
    // The whole reason for a popup: it can be blown up past the panel's width.
    const expand = within(dialog).getByRole('button', { name: 'Expand preview' });
    fireEvent.click(expand);
    expect(within(dialog).getByRole('button', { name: 'Shrink preview' })).toBeInTheDocument();
    // Expanded has to cancel the stylesheet's clamps, or Expand would do nothing.
    expect(dialog.style.maxWidth).toBe('none');
    expect(dialog.style.maxHeight).toBe('none');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Shrink preview' }));
    expect(within(dialog).getByRole('button', { name: 'Expand preview' })).toBeInTheDocument();
    expect(dialog.style.maxWidth).toBe('920px');
    fireEvent.keyDown(dialog, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'chart.png' })).not.toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: 'Preview statement.pdf' }));
    const pdfDialog = await screen.findByRole('dialog', { name: 'statement.pdf' });
    // Scripts may run (Firefox's PDF viewer needs them) but the frame must never
    // regain the journal's origin — no allow-same-origin, no allow-top-navigation.
    const pdfFrame = within(pdfDialog).getByTitle('Preview of statement.pdf');
    const tokens = (pdfFrame.getAttribute('sandbox') || '').split(/\s+/);
    expect(tokens).not.toContain('allow-same-origin');
    expect(tokens).not.toContain('allow-top-navigation');
    fireEvent.click(within(pdfDialog).getByRole('button', { name: 'Close preview' }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'statement.pdf' })).not.toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: 'Preview fills.csv' }));
    const textDialog = await screen.findByRole('dialog', { name: 'fills.csv' });
    expect(await within(textDialog).findByText(/date,ticker,pnl/)).toBeInTheDocument();
    expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/attachments/3/download'),
      expect.objectContaining({ headers: { Range: expect.stringContaining('bytes=0-') } }),
    );
    fireEvent.click(within(textDialog).getByRole('button', { name: 'Close preview' }));

    expect(screen.queryByRole('button', { name: 'Preview journal.xlsx' })).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Download journal.xlsx' })).toHaveAttribute(
      'href', 'http://mocked.invalid/api/attachments/4/download',
    );
  });

  test('a server refusal is shown verbatim instead of a dead-end sentence', async () => {
    attachmentsApi.upload.mockImplementation(() => Promise.reject({
      response: { status: 404, data: { detail: 'Trade not found' } },
    }));
    await renderTrade();

    pasteFiles({ files: [new File(['x'], '', { type: 'image/png' })], items: [] });
    await screen.findByRole('alert');
    expect(screen.getByRole('alert')).toHaveTextContent('Trade not found');
  });

  test('a backend that never answers is named, not blamed on the file', async () => {
    attachmentsApi.upload.mockImplementation(() => Promise.reject(new Error('Network Error')));
    await renderTrade();

    pasteFiles({ files: [new File(['x'], '', { type: 'image/png' })], items: [] });
    await screen.findByRole('alert');
    expect(screen.getByRole('alert')).toHaveTextContent(/backend did not answer/);
    expect(screen.getByRole('alert')).not.toHaveTextContent('could not be attached');
  });

  test('ordinary text paste and typing in a field are left alone', async () => {
    await renderTrade();

    // No files: a text paste must fall through to whatever wanted it.
    const textEvent = new Event('paste', { bubbles: true, cancelable: true });
    Object.defineProperty(textEvent, 'clipboardData', { value: { files: [] } });
    document.body.dispatchEvent(textEvent);
    expect(textEvent.defaultPrevented).toBe(false);
    expect(screen.getByRole('tab', { name: 'Files' })).toHaveAttribute('aria-selected', 'false');

    // Focus in a field: never hijack, even with files attached.
    const input = document.createElement('input');
    document.body.appendChild(input);
    const fieldEvent = new Event('paste', { bubbles: true, cancelable: true });
    Object.defineProperty(fieldEvent, 'clipboardData', {
      value: { files: [new File(['x'], 'note.png', { type: 'image/png' })] },
    });
    input.dispatchEvent(fieldEvent);
    expect(fieldEvent.defaultPrevented).toBe(false);
    expect(attachmentsApi.upload).not.toHaveBeenCalled();
  });
});

test('header keeps every page, Settings, Import, Add Trade and a labeled Brain entry visible', async () => {
  await renderApp();
  for (const label of ['Dashboard', 'Trade View', 'Calendar', 'Day Review', 'Reports', 'Diary', 'Help', 'Settings']) {
    expect(within(nav()).getByRole('button', { name: label })).toBeVisible();
  }
  const banner = screen.getByRole('banner');
  expect(within(banner).getByRole('button', { name: /^Import$/ })).toBeVisible();
  expect(within(banner).getByRole('button', { name: /Add Trade/ })).toBeVisible();
  expect(within(banner).getByRole('button', { name: /Brain/ })).toBeVisible();
});

test('Brain opens from the header as a dialog and closes on Escape', async () => {
  await renderApp();
  const trigger = within(screen.getByRole('banner')).getByRole('button', { name: /Brain/ });
  fireEvent.click(trigger);
  const dialog = await screen.findByRole('dialog', { name: /Brain/i });
  expect(dialog).toBeVisible();
  fireEvent.keyDown(dialog, { key: 'Escape' });
  await waitFor(() => expect(screen.queryByRole('dialog', { name: /Brain/i })).not.toBeInTheDocument());
});

test('account menu keeps selection, rename and new-account controls', async () => {
  await renderApp();
  fireEvent.click(screen.getByRole('button', { name: 'Account: All Accounts' }));
  const menu = screen.getByRole('menu', { name: 'Accounts' });
  expect(within(menu).getByRole('menuitemradio', { name: 'All Accounts' })).toBeInTheDocument();
  expect(within(menu).getByRole('menuitemradio', { name: /Day Trading/ })).toBeInTheDocument();
  expect(within(menu).getByRole('button', { name: /New Account/ })).toBeInTheDocument();

  fireEvent.click(within(menu).getByRole('button', { name: 'Rename Day Trading' }));
  const input = within(menu).getByRole('textbox', { name: 'New name for Day Trading' });
  fireEvent.change(input, { target: { value: 'Day Trading Main' } });
  fireEvent.keyDown(input, { key: 'Enter' });
  await waitFor(() => expect(accountsApi.update).toHaveBeenCalledWith(1, { name: 'Day Trading Main' }));
});

test('choosing an account updates the shared header state', async () => {
  await renderApp();
  fireEvent.click(screen.getByRole('button', { name: 'Account: All Accounts' }));
  fireEvent.click(screen.getByRole('menuitemradio', { name: /Swing/ }));
  expect(await screen.findByRole('button', { name: 'Account: Swing' })).toBeInTheDocument();
});

test('Add Trade opens a modal dialog that closes on Escape', async () => {
  await renderApp();
  fireEvent.click(within(screen.getByRole('banner')).getByRole('button', { name: /Add Trade/ }));
  const dialog = await screen.findByRole('dialog', { name: /Add Trade/ });
  expect(within(dialog).getByRole('button', { name: /Save Trade/ })).toBeInTheDocument();
  fireEvent.keyDown(dialog, { key: 'Escape' });
  await waitFor(() => expect(screen.queryByRole('dialog', { name: /Add Trade/ })).not.toBeInTheDocument());
});

test('Reports keeps its tabs, adds Sources & Tags, and supports arrow-key navigation', async () => {
  await renderApp();
  fireEvent.click(within(nav()).getByRole('button', { name: 'Reports' }));
  const tablist = await screen.findByRole('tablist');
  const names = within(tablist).getAllByRole('tab').map(t => t.textContent.trim());
  expect(names).toEqual(['Overview', 'Setups & Strategy', 'Sources & Tags', 'Timing', 'Execution', 'Symbols', 'Psychology']);
  const overview = within(tablist).getByRole('tab', { name: 'Overview' });
  expect(overview).toHaveAttribute('aria-selected', 'true');
  fireEvent.keyDown(overview, { key: 'ArrowRight' });
  expect(within(tablist).getByRole('tab', { name: 'Setups & Strategy' })).toHaveAttribute('aria-selected', 'true');
});

test('Trade View opens Trade Details with all six tabs, back and previous/next', async () => {
  await renderApp();
  fireEvent.click(within(nav()).getByRole('button', { name: 'Trade View' }));
  await waitFor(() => expect(tradesApi.list).toHaveBeenCalled());
  // A row opens the trade. The in-place expand was removed in V3.
  const row = (await screen.findAllByText('TSLA'))[0].closest('tr');
  fireEvent.click(row);

  const tablist = await screen.findByRole('tablist', { name: 'Trade review sections' });
  const names = within(tablist).getAllByRole('tab').map(t => t.textContent.trim());
  expect(names).toEqual(['Stats', 'Strategy', 'Tags', 'Executions', 'Files', 'What If']);
  expect(screen.getByRole('button', { name: /Back to trades/ })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /Previous trade/ })).toBeDisabled();
  expect(screen.getByRole('button', { name: /Next trade/ })).toBeEnabled();
  // Trade View stays highlighted while a trade is open.
  expect(within(nav()).getByRole('button', { name: 'Trade View' })).toHaveAttribute('aria-current', 'page');

  fireEvent.click(within(tablist).getByRole('tab', { name: 'Executions' }));
  expect(screen.getByRole('button', { name: /Add Execution/ })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Edit execution 1' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Delete execution 1' })).toBeInTheDocument();
});

test('The chart names the axis of whichever feed answered', async () => {
  // Same chart, two feeds: imported bars are UTC, the remote equity feed is
  // ET. Leaving the label off let the same panel read either way silently.
  chartApi.get.mockImplementation(() => Promise.resolve({ data: { source: 'local', bars: [], warning: null } }));
  await renderApp();
  fireEvent.click(within(nav()).getByRole('button', { name: 'Trade View' }));
  const row = (await screen.findAllByText('TSLA'))[0].closest('tr');
  fireEvent.click(row);
  expect(await screen.findByText('UTC axis')).toBeInTheDocument();

  chartApi.get.mockImplementation(() => Promise.resolve({ data: { bars: [], warning: 'No chart data in tests' } }));
  fireEvent.click(screen.getByRole('button', { name: /Back to trades/ }));
  const other = (await screen.findAllByText('TSLA'))[0].closest('tr');
  fireEvent.click(other);
  expect(await screen.findByText('ET axis')).toBeInTheDocument();
});

test('Import keeps broker CSV import and diary analysis, with keyboard dropzones', async () => {
  await renderApp();
  fireEvent.click(within(screen.getByRole('banner')).getByRole('button', { name: /^Import$/ }));
  expect(await screen.findByRole('heading', { name: /Import Broker CSV/ })).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: /Analyze Trading Diary/ })).toBeInTheDocument();
  // Both supported brokers stay selectable, with auto-detect as the default.
  const broker = screen.getByLabelText('Broker');
  expect(broker).toHaveValue('auto');
  expect(within(broker).getByRole('option', { name: /Interactive Brokers/ })).toBeInTheDocument();
  expect(within(broker).getByRole('option', { name: /Thinkorswim/ })).toBeInTheDocument();
  expect(within(broker).getByRole('option', { name: /MetaTrader 5/ })).toBeInTheDocument();
  // Broker CSV, M1 bars and diary — each with a keyboard-reachable dropzone.
  const dropzones = screen.getAllByRole('button', { name: /Press Enter to browse/ });
  expect(dropzones).toHaveLength(3);
  dropzones.forEach(z => expect(z).toHaveAttribute('tabindex', '0'));
});

test('Import offers a bar dropzone alongside the deal and diary ones', async () => {
  await renderApp();
  fireEvent.click(within(screen.getByRole('banner')).getByRole('button', { name: /^Import$/ }));
  expect(await screen.findByRole('heading', { name: /Import M1 Bars/ })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /Drop an M1 bar CSV/ })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /Import Bars/ })).toBeInTheDocument();
  expect(screen.getByText(/ExportBarsCSV/)).toBeInTheDocument();
});

test('Choosing MetaTrader 5 warns that the server timezone must be set first', async () => {
  await renderApp();
  fireEvent.click(within(screen.getByRole('banner')).getByRole('button', { name: /^Import$/ }));
  expect(await screen.findByRole('heading', { name: /Import Broker CSV/ })).toBeInTheDocument();

  const broker = screen.getByLabelText('Broker');
  // Daylight saving is only correct if the zone is chosen before the first
  // import — later changes would shift dates under already-grouped trades.
  fireEvent.change(broker, { target: { value: 'mt5' } });
  expect(broker).toHaveValue('mt5');
  // Scoped to the broker help: the bars card beside it mentions a server
  // timezone too, so an unscoped match would hit either of two cards.
  const help = screen.getByRole('heading', { name: /Import Broker CSV/ }).closest('section');
  expect(within(help).getByText(/server timezone/i)).toBeInTheDocument();
  expect(within(help).getByText(/before the first import/i)).toBeInTheDocument();
});

test('Help lists the metric reference and the feature guide', async () => {
  await renderApp();
  fireEvent.click(within(nav()).getByRole('button', { name: 'Help' }));
  expect(await screen.findByRole('heading', { name: 'Help and Reference' })).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: 'Dashboard KPIs' })).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: 'Features' })).toBeInTheDocument();
});

test('Calendar keeps the Month and Year views', async () => {
  await renderApp();
  fireEvent.click(within(nav()).getByRole('button', { name: 'Calendar' }));
  expect(await screen.findByRole('button', { name: 'Month' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Year' })).toBeInTheDocument();
});

test('Day Review keeps the loss-streak alert and its Dismiss control', async () => {
  const loss = (id, time, pnl) => ({
    id, account_id: 1, trade_group: `g${id}`, date: '2026-09-10', ticker: `L${id}`, instrument_type: 'STOCK',
    side: 'LONG', net_pnl: pnl, gross_pnl: pnl, commissions: 0,
    executions: [{ date: '2026-09-10', time, action: 'BOT', qty: 1, price: 10, commission: 0 }],
  });
  tradesApi.list.mockImplementation(() => Promise.resolve({ data: [loss(1, '09:40:00', -10), loss(2, '10:10:00', -20), loss(3, '11:00:00', -30)] }));
  await renderApp();
  fireEvent.click(within(nav()).getByRole('button', { name: 'Day Review' }));
  const alert = await screen.findByRole('alert');
  expect(alert).toHaveTextContent(/3 losses in a row/);
  fireEvent.click(within(alert).getByRole('button', { name: 'Dismiss loss-streak alert' }));
  await waitFor(() => expect(screen.queryByText(/3 losses in a row/)).not.toBeInTheDocument());
  // Previous, Next and Regenerate AI stay in the page header.
  expect(screen.getByRole('button', { name: /Previous/ })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /Next/ })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /Regenerate AI/ })).toBeInTheDocument();
});

test('Settings has Strategies, Sources, Tags, Import and Backup sections, and Tags leaves out strategy and source types', async () => {
  await renderApp();
  fireEvent.click(within(nav()).getByRole('button', { name: 'Settings' }));
  const tablist = await screen.findByRole('tablist', { name: 'Settings sections' });
  expect(within(tablist).getAllByRole('tab').map(t => t.textContent.replace(/\d+/g, '').trim()))
    .toEqual(['Strategies', 'Sources', 'Tags', 'Import', 'Backup']);
  expect(await screen.findByText('VWAP Cross')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Edit VWAP Cross' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Delete VWAP Cross' })).toBeInTheDocument();

  fireEvent.click(within(tablist).getByRole('tab', { name: /Tags/ }));
  expect(await screen.findByRole('region', { name: 'Mistakes tags' })).toBeInTheDocument();
  expect(screen.getByRole('region', { name: 'Execution tags' })).toBeInTheDocument();
  expect(screen.queryByRole('region', { name: /Strategy tags/ })).not.toBeInTheDocument();
  expect(screen.queryByRole('region', { name: /Source tags/ })).not.toBeInTheDocument();
});

test('Settings merges one strategy into another', async () => {
  await renderApp();
  fireEvent.click(within(nav()).getByRole('button', { name: 'Settings' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Merge Continuation RS into another strategy' }));
  fireEvent.change(screen.getByRole('combobox', { name: /Merge "Continuation RS" into/ }), { target: { value: 'VWAP Cross' } });
  fireEvent.click(screen.getByRole('button', { name: 'Merge' }));
  await waitFor(() => expect(libraryApi.merge).toHaveBeenCalledWith({
    kind: 'strategy', tag_type: '', source_name: 'Continuation RS', target_name: 'VWAP Cross',
  }));
  expect(await screen.findByRole('status')).toHaveTextContent(/2 trades moved/);
});

test('Settings Import names the MT5 server timezone and saves it', async () => {
  await renderApp();
  fireEvent.click(within(nav()).getByRole('button', { name: 'Settings' }));
  const tablist = await screen.findByRole('tablist', { name: 'Settings sections' });
  fireEvent.click(within(tablist).getByRole('tab', { name: /Import/ }));

  expect(await screen.findByRole('region', { name: 'MT5 import settings' })).toBeInTheDocument();
  await waitFor(() => expect(mt5TimezoneApi.get).toHaveBeenCalled());
  // The env value is active but not saved yet, and the zone's clock changes are shown.
  expect(screen.getByText(/using the environment value/)).toBeInTheDocument();
  expect(screen.getByRole('columnheader', { name: 'Offset after' })).toBeInTheDocument();

  fireEvent.change(screen.getByRole('combobox', { name: 'Broker server timezone' }), {
    target: { value: 'Europe/London' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Save timezone' }));
  await waitFor(() => expect(mt5TimezoneApi.put).toHaveBeenCalledWith('Europe/London'));
  expect(await screen.findByRole('status')).toHaveTextContent(/Saved Europe\/London/);
});

test('Settings Backup names a folder before backing up, and restores beside the archive', async () => {
  await renderApp();
  fireEvent.click(within(nav()).getByRole('button', { name: 'Settings' }));
  const tablist = await screen.findByRole('tablist', { name: 'Settings sections' });
  fireEvent.click(within(tablist).getByRole('tab', { name: /Backup/ }));

  const section = await screen.findByRole('region', { name: 'Backup and export settings' });
  expect(within(section).getByText(/Choose a folder first/)).toBeInTheDocument();
  // Backups stay disabled until a folder is chosen: an archive has to land
  // somewhere, and a silent default folder would be a surprise.
  expect(within(section).getByRole('button', { name: 'Create backup' })).toBeDisabled();

  fireEvent.change(within(section).getByLabelText(/Backup folder/), {
    target: { value: 'C:\\journal\\backups' },
  });
  fireEvent.click(within(section).getByRole('button', { name: 'Save folder' }));
  await waitFor(() => expect(backupApi.setDestination).toHaveBeenCalledWith('C:\\journal\\backups'));

  fireEvent.click(await within(section).findByRole('button', { name: 'Create backup' }));
  await waitFor(() => expect(backupApi.create).toHaveBeenCalled());
  expect(await within(section).findByRole('status')).toHaveTextContent(/tdjournal-backup\.zip/);

  // Both export formats are plain downloads, so nothing here waits on JS.
  expect(within(section).getByRole('link', { name: 'Export JSON' }))
    .toHaveAttribute('href', expect.stringContaining('fmt=json'));
  expect(within(section).getByRole('link', { name: 'Export CSV' }))
    .toHaveAttribute('href', expect.stringContaining('fmt=csv'));
});

test('Settings delete asks to reassign and can leave trades blank', async () => {
  await renderApp();
  fireEvent.click(within(nav()).getByRole('button', { name: 'Settings' }));
  fireEvent.click(await screen.findByRole('tab', { name: /Sources/ }));
  fireEvent.click(await screen.findByRole('button', { name: 'Delete OneOption' }));
  expect(screen.getByRole('combobox', { name: /1 trade use "OneOption". Reassign them to/ })).toHaveValue('');
  fireEvent.click(screen.getAllByRole('button', { name: 'Delete' }).find(b => b.className.includes('btn-danger')));
  await waitFor(() => expect(libraryApi.remove).toHaveBeenCalledWith({
    kind: 'source', tag_type: '', name: 'OneOption', reassign_to: null,
  }));
});

const todayISO = () => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
};

test('Open Positions shows the remaining quantity, not the quantity entered', async () => {
  await renderApp();
  const row = (await screen.findByText('GOOG')).closest('tr');
  // 300 bought, 100 sold.
  expect(within(row).getByText('200')).toBeInTheDocument();
  expect(within(row).getByText(/of 300/)).toBeInTheDocument();
});

test('Recording an exit defaults to today, sends the entered time and fees, and refreshes', async () => {
  await renderApp();
  const row = (await screen.findByText('GOOG')).closest('tr');
  fireEvent.click(within(row).getByRole('button', { name: 'Close' }));

  const date = screen.getByLabelText('Exit date');
  expect(date).toHaveValue(todayISO());
  fireEvent.change(screen.getByLabelText('Exit time'), { target: { value: '15:45' } });
  fireEvent.change(screen.getByLabelText('Exit price'), { target: { value: '107.5' } });
  fireEvent.change(screen.getByLabelText('Fees'), { target: { value: '1.25' } });

  const callsBefore = tradesApi.list.mock.calls.length;
  fireEvent.click(screen.getByRole('button', { name: 'Record exit' }));
  await waitFor(() => expect(tradesApi.addExecution).toHaveBeenCalledWith(103, {
    action: 'SOLD', qty: 200, price: 107.5, date: todayISO(), time: '15:45:00', commission: 1.25,
  }));
  // The rest of the dashboard reloads instead of showing stale totals.
  await waitFor(() => expect(tradesApi.list.mock.calls.length).toBeGreaterThan(callsBefore));
});

test('Recording an exit refuses a date before the last fill', async () => {
  await renderApp();
  const row = (await screen.findByText('GOOG')).closest('tr');
  fireEvent.click(within(row).getByRole('button', { name: 'Close' }));
  fireEvent.change(screen.getByLabelText('Exit date'), { target: { value: '2026-09-08' } });
  fireEvent.change(screen.getByLabelText('Exit price'), { target: { value: '107.5' } });
  fireEvent.click(screen.getByRole('button', { name: 'Record exit' }));
  expect(await screen.findByRole('alert')).toHaveTextContent(/cannot be earlier than the last fill on 2026-09-09/);
  expect(tradesApi.addExecution).not.toHaveBeenCalled();
});

test('A failed dashboard load keeps the page and offers Retry', async () => {
  kpisApi.get.mockImplementation(() => Promise.reject(new Error('Network Error')));
  await renderApp();
  const alert = await screen.findByRole('alert');
  expect(alert).toHaveTextContent(/Could not load the dashboard: Network Error/);
  // The shell stays: title and the date filter are still there.
  expect(screen.getByRole('heading', { name: 'Dashboard' })).toBeInTheDocument();
  expect(within(screen.getByRole('main')).getByRole('button', { name: /All time/ })).toBeInTheDocument();

  kpisApi.get.mockImplementation(() => Promise.resolve({ data: { total_net_pnl: 100, daily_pnl: [], by_strategy: [] } }));
  fireEvent.click(within(alert).getByRole('button', { name: 'Retry' }));
  await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
});

test('Goals save failure is shown and keeps the panel open', async () => {
  goalsApi.put.mockImplementation(() => Promise.reject(new Error('Server error')));
  await renderApp();
  fireEvent.click(screen.getByRole('button', { name: 'Edit goals' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Save' }));
  expect(await screen.findByRole('alert')).toHaveTextContent(/Could not save goals: Server error/);
  expect(screen.getByRole('heading', { name: 'Goals' })).toBeInTheDocument();
});
