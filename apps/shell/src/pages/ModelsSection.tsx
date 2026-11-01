import { useCallback, useEffect, useMemo, useState } from 'react'
import type {
  AppError,
  Capability,
  Provider,
  ProviderCreate,
  ProviderKind,
  ProviderTestResult,
  ProvidersResponse,
  RoutingMode,
} from '../types'
import {
  createProvider,
  deleteProvider,
  listProviders,
  testProvider,
  toAppError,
  updateProvider,
  updateRouting,
} from '../lib/api'
import { IconAlert, IconBolt, IconCheck, IconClose, IconEdit, IconPlus, IconRefresh, IconTrash } from '../icons'

const CAPABILITIES: { id: Capability; label: string }[] = [
  { id: 'chat', label: '对话' },
  { id: 'extract', label: '记忆提取' },
  { id: 'image', label: '生图' },
  { id: 'tts', label: '语音' },
]

const TASK_LABELS: Record<string, string> = {
  chat: '对话',
  extract: '记忆提取',
  image: '生图',
  tts: '语音',
}

interface ModelsSectionProps {
  cfg: { baseUrl: string; token: string }
  onToast: (tone: 'info' | 'success' | 'error', message: string) => void
}

interface FormState {
  id: string | null
  name: string
  kind: ProviderKind
  base_url: string
  model: string
  api_key: string
  clearKey: boolean
  capabilities: Capability[]
}

function emptyForm(): FormState {
  return {
    id: null,
    name: '',
    kind: 'openai_compatible',
    base_url: '',
    model: '',
    api_key: '',
    clearKey: false,
    capabilities: ['chat'],
  }
}

function statusChip(p: Provider) {
  switch (p.status) {
    case 'ok':
      return <span className="chip chip--mint">正常</span>
    case 'down':
      return <span className="chip chip--rose">连不上</span>
    case 'unknown':
      return <span className="chip chip--amber">状态未知</span>
    default:
      return <span className="chip">未检测</span>
  }
}

function ErrorBlock({ error }: { error: AppError }) {
  return (
    <div className="alert alert--error" role="alert">
      <IconAlert size={16} />
      <div>
        <strong>{error.message}</strong>
        {error.hint && <span>{error.hint}</span>}
        {error.problems && error.problems.length > 0 && (
          <ul>
            {error.problems.map((p, i) => <li key={i}>{p}</li>)}
          </ul>
        )}
      </div>
    </div>
  )
}

export function ModelsSection({ cfg, onToast }: ModelsSectionProps) {
  const [data, setData] = useState<ProvidersResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<AppError | null>(null)
  const [form, setForm] = useState<FormState | null>(null)
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState<AppError | null>(null)
  const [testing, setTesting] = useState<string | null>(null)
  const [results, setResults] = useState<Record<string, ProviderTestResult>>({})
  const [confirmDelete, setConfirmDelete] = useState<Provider | null>(null)
  const [routingBusy, setRoutingBusy] = useState(false)

  const maxEnabled = data?.limits.max_enabled ?? 4

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setData(await listProviders(cfg))
    } catch (err) {
      setError(toAppError(err))
    } finally {
      setLoading(false)
    }
  }, [cfg])

  useEffect(() => {
    void load()
  }, [load])

  const byId = useMemo(() => {
    const map: Record<string, Provider> = {}
    for (const p of data?.items ?? []) map[p.id] = p
    return map
  }, [data])

  const routing = data?.routing
  const mode: RoutingMode = routing?.mode ?? 'single'
  // 契约：concurrent 模式最多启用 max_enabled 个
  const selected = mode === 'single'
    ? (routing?.active_provider_id ? [routing.active_provider_id] : [])
    : (routing?.enabled_ids ?? [])

  const toggleProvider = (id: string) => {
    if (!data || routingBusy) return
    const cur = mode === 'single'
      ? (routing?.active_provider_id ? [routing.active_provider_id] : [])
      : [...(routing?.enabled_ids ?? [])]
    const next = cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]

    if (mode === 'concurrent' && next.length > maxEnabled) {
      onToast('error', '最多只能同时启用 ' + maxEnabled + ' 个提供商')
      return
    }
    if (mode === 'single' && next.length > 1) {
      cur.length = 0
      cur.push(id)
    }
    void saveRouting(mode, mode === 'single' ? { active_provider_id: next[0] ?? null } : { enabled_ids: next })
  }

  const saveRouting = async (
    nextMode: RoutingMode,
    payload: { active_provider_id?: string | null; enabled_ids?: string[] },
  ) => {
    if (!data) return
    setRoutingBusy(true)
    const previous = data
    try {
      await updateRouting(cfg, { mode: nextMode, ...payload })
      const fresh = await listProviders(cfg)
      setData(fresh)
    } catch (err) {
      setData(previous)
      setError(toAppError(err))
    } finally {
      setRoutingBusy(false)
    }
  }

  const switchMode = (nextMode: RoutingMode) => {
    if (!data || nextMode === mode) return
    if (nextMode === 'single') {
      const first = routing?.enabled_ids?.[0] ?? data.items.find((p) => !p.is_builtin)?.id ?? data.items[0]?.id ?? null
      void saveRouting('single', { active_provider_id: first })
    } else {
      const ids = routing?.active_provider_id ? [routing.active_provider_id] : []
      void saveRouting('concurrent', { enabled_ids: ids })
    }
  }

  const openCreate = () => {
    setFormError(null)
    setForm(emptyForm())
  }

  const openEdit = (p: Provider) => {
    setFormError(null)
    setForm({
      id: p.id,
      name: p.name,
      kind: p.kind,
      base_url: p.base_url,
      model: p.model,
      api_key: '',
      clearKey: false,
      capabilities: p.capabilities.filter((c): c is Capability =>
        CAPABILITIES.some((cap) => cap.id === c),
      ),
    })
  }

  const submitForm = async () => {
    if (!form || saving) return
    setFormError(null)

    // 前端先做一轮友好校验，减少无谓的 422（后端仍会复核）
    const name = form.name.trim()
    const baseUrl = form.base_url.trim()
    const model = form.model.trim()
    const problems: string[] = []
    if (!name) problems.push('名称不能为空')
    if (name.length > 40) problems.push('名称最多 40 个字符')
    if (!baseUrl) problems.push('接口地址不能为空')
    else if (!/^https?:\/\//i.test(baseUrl)) problems.push('接口地址必须以 http:// 或 https:// 开头')
    if (!model) problems.push('模型名不能为空')
    if (form.capabilities.length === 0) problems.push('至少选择一个用途')
    if (problems.length > 0) {
      setFormError({ kind: 'validation', message: '填写的内容还需要调整', problems })
      return
    }

    setSaving(true)
    try {
      if (form.id) {
        // api_key 语义：留空且未勾选清除 = 不修改
        const patch: Record<string, unknown> = {
          name,
          kind: form.kind,
          base_url: baseUrl,
          model,
          capabilities: form.capabilities,
        }
        if (form.clearKey) patch.api_key = null
        else if (form.api_key.trim()) patch.api_key = form.api_key.trim()
        await updateProvider(cfg, form.id, patch)
        onToast('success', '已保存修改')
      } else {
        const body: ProviderCreate = {
          name,
          kind: form.kind,
          base_url: baseUrl,
          model,
          capabilities: form.capabilities,
        }
        if (form.api_key.trim()) body.api_key = form.api_key.trim()
        await createProvider(cfg, body)
        onToast('success', '提供商已添加')
      }
      setForm(null)
      await load()
    } catch (err) {
      setFormError(toAppError(err))
    } finally {
      setSaving(false)
    }
  }

  const runTest = async (p: Provider) => {
    setTesting(p.id)
    try {
      const result = await testProvider(cfg, p.id)
      setResults((cur) => ({ ...cur, [p.id]: result }))
      if (result.ok) onToast('success', p.name + ' 连通正常（' + result.latency_ms + ' ms）')
      else onToast('error', p.name + ' 连不上')
      await load()
    } catch (err) {
      setError(toAppError(err))
    } finally {
      setTesting(null)
    }
  }

  const doDelete = async () => {
    if (!confirmDelete) return
    try {
      await deleteProvider(cfg, confirmDelete.id)
      onToast('success', '已删除 ' + confirmDelete.name)
      setConfirmDelete(null)
      await load()
    } catch (err) {
      setError(toAppError(err))
      setConfirmDelete(null)
    }
  }

  return (
    <div className="settings-block" aria-label="模型">
      <div className="models-head">
        <div>
          <h3 className="settings-h3" style={{ marginTop: 0 }}>模型提供商</h3>
          <p className="field-help">灵鲸鲸用哪个模型来思考</p>
        </div>
        <div className="page-actions">
          <button type="button" className="btn btn--icon" onClick={() => void load()} disabled={loading} aria-label="刷新">
            <span className={loading ? 'spin' : undefined} style={{ display: 'inline-flex' }}>
              <IconRefresh size={16} />
            </span>
          </button>
          <button type="button" className="btn btn--primary" onClick={openCreate}>
            <IconPlus size={15} />
            添加
          </button>
        </div>
      </div>

      <div>
        {error && <div style={{ marginBottom: 'var(--s4)' }}><ErrorBlock error={error} /></div>}

        {loading && !data ? (
          <div className="empty"><span className="spinner" /><p>正在读取模型配置…</p></div>
        ) : !data ? null : (
          <>
            <div className="routing-panel">
              <div className="routing-head">
                <div className="routing-label">使用方式</div>
                <div className="segmented" role="group" aria-label="使用方式">
                  <button
                    type="button"
                    className={mode === 'single' ? 'is-on' : undefined}
                    onClick={() => switchMode('single')}
                    disabled={routingBusy}
                  >
                    单个
                  </button>
                  <button
                    type="button"
                    className={mode === 'concurrent' ? 'is-on' : undefined}
                    onClick={() => switchMode('concurrent')}
                    disabled={routingBusy}
                  >
                    并发
                  </button>
                </div>
              </div>

              <p className="field-help">
                {mode === 'single'
                  ? '所有任务都由同一个提供商完成。'
                  : '最多同时启用 ' + maxEnabled + ' 个提供商，后端按任务类型自动分配。'}
              </p>

              <div className="assign-grid">
                {Object.keys(TASK_LABELS).map((task) => {
                  const owner = routing?.assignments?.[task]
                  const ownerName = owner ? byId[owner]?.name : undefined
                  return (
                    <div key={task} className={'assign-cell' + (ownerName ? '' : ' assign-cell--empty')}>
                      <span>{TASK_LABELS[task]}</span>
                      <span>{ownerName ?? '未分配'}</span>
                    </div>
                  )
                })}
              </div>
            </div>

            <div className="provider-list">
              {data.items.length === 0 && (
                <div className="empty">
                  <strong>还没有提供商</strong>
                  <p>点右上角「添加」，填上接口地址与模型名即可。</p>
                </div>
              )}

              {data.items.map((p) => {
                const picked = selected.includes(p.id)
                const result = results[p.id]
                const full = mode === 'concurrent' && !picked && selected.length >= maxEnabled
                return (
                  <div key={p.id} className={'provider' + (picked ? ' is-selected' : '')}>
                    <div className="provider-top">
                      <button
                        type="button"
                        className={'check' + (picked ? ' is-on' : '')}
                        onClick={() => toggleProvider(p.id)}
                        disabled={routingBusy || full}
                        aria-pressed={picked}
                        aria-label={(picked ? '取消启用 ' : '启用 ') + p.name}
                        title={full ? '已达上限 ' + maxEnabled + ' 个' : undefined}
                      >
                        <IconCheck size={13} />
                      </button>
                      <div style={{ minWidth: 0, flex: 1 }}>
                        <div className="provider-name">
                          {p.name}
                          {p.is_builtin && <span className="chip">内置</span>}
                          {statusChip(p)}
                          {!p.enabled && <span className="chip">已停用</span>}
                        </div>
                        <div className="provider-meta">
                          {p.kind} · {p.model || '未填模型'} · {p.base_url}
                        </div>
                      </div>
                      <div className="provider-actions">
                        <button
                          type="button"
                          className="icon-btn"
                          onClick={() => void runTest(p)}
                          disabled={testing === p.id}
                          aria-label={'测试 ' + p.name}
                          title="测试连通性"
                        >
                          {testing === p.id ? <span className="spinner" /> : <IconBolt size={16} />}
                        </button>
                        <button
                          type="button"
                          className="icon-btn"
                          onClick={() => openEdit(p)}
                          aria-label={'编辑 ' + p.name}
                          title="编辑"
                        >
                          <IconEdit size={16} />
                        </button>
                        <button
                          type="button"
                          className="icon-btn icon-btn--danger"
                          onClick={() => setConfirmDelete(p)}
                          disabled={p.is_builtin}
                          aria-label={'删除 ' + p.name}
                          title={p.is_builtin ? '内置提供商不能删除' : '删除'}
                        >
                          <IconTrash size={16} />
                        </button>
                      </div>
                    </div>

                    <div className="provider-foot">
                      {p.capabilities.map((c) => (
                        <span key={c} className="chip">{TASK_LABELS[c] ?? c}</span>
                      ))}
                      {p.has_api_key && <span className="chip chip--aqua">已设置密钥</span>}
                      {result && (
                        <span className={'chip ' + (result.ok ? 'chip--mint' : 'chip--rose')}>
                          {result.ok
                            ? '连通 ' + result.latency_ms + ' ms' + (result.models.length ? ' · ' + result.models.length + ' 个模型' : '')
                            : '失败' + (result.error ? '：' + result.error : '')}
                        </span>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          </>
        )}
      </div>

      {form && (
        <div className="modal-scrim" onClick={(e) => { if (e.target === e.currentTarget) setForm(null) }}>
          <div className="modal" role="dialog" aria-modal="true" aria-label={form.id ? '编辑提供商' : '添加提供商'}>
            <div className="modal-head">
              <div>
                <h2>{form.id ? '编辑提供商' : '添加提供商'}</h2>
                <p>密钥只写不读，保存后不会再显示</p>
              </div>
              <button type="button" className="icon-btn" onClick={() => setForm(null)} aria-label="关闭">
                <IconClose size={16} />
              </button>
            </div>

            {formError && <ErrorBlock error={formError} />}

            <div className="field">
              <label htmlFor="p-name">名称</label>
              <input id="p-name" className="input" value={form.name} maxLength={40}
                onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="例如：本地 Ollama" />
            </div>

            <div className="field">
              <label htmlFor="p-kind">类型</label>
              <select id="p-kind" className="select" value={form.kind}
                onChange={(e) => setForm({ ...form, kind: e.target.value as ProviderKind })}>
                <option value="openai_compatible">openai_compatible（通用 OpenAI 兼容）</option>
                <option value="ollama">ollama</option>
              </select>
            </div>

            <div className="field">
              <label htmlFor="p-url">接口地址</label>
              <input id="p-url" className="input" value={form.base_url} spellCheck={false}
                onChange={(e) => setForm({ ...form, base_url: e.target.value })} placeholder="http://127.0.0.1:11434/v1" />
            </div>

            <div className="field">
              <label htmlFor="p-model">模型名</label>
              <input id="p-model" className="input" value={form.model} spellCheck={false}
                onChange={(e) => setForm({ ...form, model: e.target.value })} placeholder="qwen2.5:7b" />
            </div>

            <div className="field">
              <label htmlFor="p-key">API 密钥（可选）</label>
              <input id="p-key" className="input" type="password" value={form.api_key} autoComplete="off"
                onChange={(e) => setForm({ ...form, api_key: e.target.value, clearKey: false })}
                placeholder={form.id ? '留空表示不修改' : '本地模型通常不需要'} />
              {form.id && (
                <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: 'var(--text-3)' }}>
                  <input type="checkbox" checked={form.clearKey}
                    onChange={(e) => setForm({ ...form, clearKey: e.target.checked, api_key: '' })} />
                  清除已保存的密钥
                </label>
              )}
            </div>

            <div className="field">
              <label>用途</label>
              <div className="cap-row">
                {CAPABILITIES.map((cap) => {
                  const on = form.capabilities.includes(cap.id)
                  return (
                    <button key={cap.id} type="button" className={'cap-toggle' + (on ? ' is-on' : '')}
                      aria-pressed={on}
                      onClick={() => setForm({
                        ...form,
                        capabilities: on
                          ? form.capabilities.filter((c) => c !== cap.id)
                          : [...form.capabilities, cap.id],
                      })}>
                      {cap.label}
                    </button>
                  )
                })}
              </div>
            </div>

            <div className="modal-actions">
              <button type="button" className="btn btn--ghost" onClick={() => setForm(null)}>取消</button>
              <button type="button" className="btn btn--primary" onClick={() => void submitForm()} disabled={saving}>
                {saving ? '保存中…' : '保存'}
              </button>
            </div>
          </div>
        </div>
      )}

      {confirmDelete && (
        <div className="modal-scrim" onClick={(e) => { if (e.target === e.currentTarget) setConfirmDelete(null) }}>
          <div className="modal" role="alertdialog" aria-modal="true" aria-label="确认删除">
            <div className="modal-head">
              <div>
                <h2>删除「{confirmDelete.name}」？</h2>
                <p>删除后无法恢复，正在使用它的对话会自动换回其他提供商。</p>
              </div>
            </div>
            <div className="modal-actions">
              <button type="button" className="btn btn--ghost" onClick={() => setConfirmDelete(null)}>取消</button>
              <button type="button" className="btn btn--primary" onClick={() => void doDelete()}>确认删除</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
