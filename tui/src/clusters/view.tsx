/**
 * DELFIN's clusters and what runs in the background: two screens of the
 * window's own, from the agent service's clusters.list and jobs.list
 * (serviceVersion 4 and later).
 *
 *   /clusters   every cluster of remote.clusters: logged in, logged out or
 *               refused, its target, and while logged in the session
 *               budget and the active jobs; the selected one says what is
 *               wrong (a refused one's problems, an error)
 *                 ↑ ↓      select           enter / l   log in (login.tsx)
 *                 o        log out          r           ask again
 *                 esc      back
 *   /jobs       the agents' background work (a group per session), the
 *               jobs DELFIN's job monitor watches, and the active jobs on
 *               each logged-in cluster; a source that could not be read is
 *               said at the end, never shown as empty. Read-only.
 *                 ↑ ↓      scroll           r           ask again
 *                 esc      back
 *
 * A screen takes the agent view's place (the whole terminal but the
 * prompt) and every key while it is open, and asks again every 5 s. The
 * service looks at the clusters over ssh (with a cache): an answer may
 * take some seconds, and "asking…" is said until the first one is in.
 */
import type { KeyEvent } from "@opentui/core"
import { For, createEffect, onCleanup, type JSX } from "solid-js"
import { createStore } from "solid-js/store"
import type { Keymap } from "../keymap/keymap"
import { oneLine } from "../store/state"
import { fit, width } from "../text"
import type { Theme } from "../theme/theme"

export type Screen = "clusters" | "jobs"

/** The first service that answers clusters.list, cluster.login/logout and jobs.list. */
export const CLUSTERS_SERVICE_VERSION = 4

/** How often an open screen asks the service again. */
export const CLUSTERS_POLL_MS = 5000

/** A cluster as clusters.list has it (delfin/agent/service/clusters_feed.py). */
export interface ClusterRow {
  name: string
  target: string
  state: "refused" | "logged_out" | "logged_in"
  /** Why DELFIN does not use it (a refused one). */
  problems: string[]
  /** What a login asks for: the masked field's fields. */
  requiresPassword: boolean
  requiresOtp: boolean
  /** The session budget while logged in, when the cluster says one. */
  budget: { used: number; limit: number; unit: string } | null
  /** The user's active jobs there while logged in. */
  jobs: number | null
  error: string | null
}

export interface JobRow {
  id: string
  kind: string
  label: string
  state: string
  detail: string
}

export interface JobGroup {
  source: "agent" | "monitor" | "cluster"
  /** The session's (agent), "DELFIN's job monitor", the cluster's. */
  title: string
  rows: JobRow[]
  /** A cluster's: how many of its jobs ended recently. */
  finished: number
}

/** A field as one plain line, whatever the service sent. */
const clean = (value: unknown, max: number) =>
  oneLine(typeof value === "string" ? value : value == null ? "" : String(value), max)

const STATES = new Set(["refused", "logged_out", "logged_in"])

/** The rows clusters.list sent, each field as its type says -- a row
 *  without a name is left out, one of an unknown state is refused. */
export function clusterRows(raw: unknown): ClusterRow[] {
  if (!Array.isArray(raw)) return []
  const out: ClusterRow[] = []
  for (const r of raw as Record<string, any>[]) {
    if (!r || typeof r !== "object" || !clean(r.name, 64)) continue
    const b = r.budget && typeof r.budget === "object" ? r.budget : null
    out.push({
      name: clean(r.name, 64), target: clean(r.target, 120),
      state: STATES.has(r.state) ? r.state : "refused",
      problems: (Array.isArray(r.problems) ? r.problems : []).map((p: unknown) => clean(p, 300)).filter(Boolean),
      requiresPassword: Boolean(r.requiresPassword), requiresOtp: Boolean(r.requiresOtp),
      budget: b && Number.isFinite(Number(b.used)) && Number.isFinite(Number(b.limit))
        ? { used: Number(b.used), limit: Number(b.limit), unit: clean(b.unit, 16) } : null,
      jobs: typeof r.jobs === "number" && Number.isFinite(r.jobs) ? r.jobs : null,
      error: clean(r.error, 300) || null,
    })
  }
  return out
}

const SOURCES = new Set(["agent", "monitor", "cluster"])

/** The groups jobs.list sent, each field as its type says. */
export function jobGroups(raw: unknown): JobGroup[] {
  if (!Array.isArray(raw)) return []
  return (raw as Record<string, any>[]).filter((g) => g && typeof g === "object").map((g) => ({
    source: SOURCES.has(g.source) ? g.source : "agent",
    title: clean(g.title, 120) || clean(g.cluster, 64) || "(untitled)",
    rows: (Array.isArray(g.rows) ? g.rows : []).filter((r: unknown) => r && typeof r === "object")
      .map((r: Record<string, unknown>) => ({ id: clean(r.id, 64), kind: clean(r.kind, 40), label: clean(r.label, 120),
                                              state: clean(r.state, 24), detail: clean(r.detail, 160) })),
    finished: Math.max(0, Number(g.finished) || 0),
  }))
}

/** Why a refused cluster cannot be logged into: its first problem -- and
 *  ctun's command when that is its host key (the service's words name it). */
export function refusal(row: ClusterRow): string {
  const why = row.problems[0] || row.error || "DELFIN does not use it yet"
  const pin = `ctun -t ${row.name} hostkey --pin`
  return `${row.name} cannot be logged into: ${why}${/host ?key/i.test(why) && !why.includes(pin) ? ` (${pin})` : ""}`
}

const num = (n: number) => Number.isInteger(n) ? String(n) : n.toFixed(1)

export function budgetText(b: ClusterRow["budget"]): string {
  return b ? `${num(b.used)}/${num(b.limit)}${b.unit ? ` ${b.unit}` : ""}` : ""
}

export interface ClustersViewState {
  open: Screen | null
  /** The first answer is not in yet. */
  loading: boolean
  /** Why the service did not answer. */
  error: string
  clusters: ClusterRow[]
  groups: JobGroup[]
  /** The sources of /jobs that could not be read. */
  errors: string[]
  /** /clusters: the selected row. */
  selected: number
  /** /jobs: the first line shown. */
  scroll: number
}

export interface ClustersViewDeps {
  request(method: string, params: Record<string, unknown>): Promise<any>
  keymap: Keymap
  say(text: string): void
  /** Enter on a logged-out cluster: the masked login field for it. */
  login(row: ClusterRow): void
}

/** A line of /jobs: a group's title, a job, a group without jobs, a source not read. */
export interface JobLine {
  kind: "title" | "row" | "empty" | "error"
  text: string
  row?: JobRow
}

export interface ClustersView {
  state: ClustersViewState
  open(screen: Screen): void
  close(): void
  /** Ask the service again (r, the poll, a login that went through). */
  refresh(): void
  /** A key while a screen is open; every key is its own. */
  handleKey(key: KeyEvent): boolean
  selectedRow(): ClusterRow | undefined
  jobLines(): JobLine[]
  /** The rows the screen has for its lines (↓ stops at the last one shown). */
  setRoom(rows: number): void
}

export function createClustersView(deps: ClustersViewDeps): ClustersView {
  const [state, setState] = createStore<ClustersViewState>({
    open: null, loading: false, error: "", clusters: [], groups: [], errors: [], selected: 0, scroll: 0,
  })
  let room = 1
  /** The screens with a request on its way: one at a time each. One
   *  asked for meanwhile (r, a login that went through) goes after it; the
   *  poll's waits for its next turn. */
  const asking = new Set<Screen>()
  const again = new Set<Screen>()

  const open = (screen: Screen) => {
    if (state.open === screen) return refresh()
    setState({ open: screen, loading: true, error: "", clusters: [], groups: [], errors: [], selected: 0, scroll: 0 })
  }
  const close = () => setState({ open: null })

  const ask = (queue: boolean) => {
    const screen = state.open
    if (!screen) return
    if (asking.has(screen)) {
      if (queue) again.add(screen)
      return
    }
    asking.add(screen)
    void deps.request(screen === "clusters" ? "clusters.list" : "jobs.list", {}).then((got) => {
      if (state.open !== screen) return
      if (screen === "clusters") setState({ clusters: clusterRows(got?.clusters), loading: false, error: "" })
      else {
        setState({ groups: jobGroups(got?.groups), loading: false, error: "",
                   errors: (Array.isArray(got?.errors) ? got.errors : []).map((e: unknown) => clean(e, 300)).filter(Boolean) })
      }
    }, (error) => {
      if (state.open === screen) setState({ loading: false, error: clean((error as Error)?.message ?? error, 300) })
    }).finally(() => {
      asking.delete(screen)
      if (again.delete(screen) && state.open === screen) refresh()
    })
  }
  const refresh = () => ask(true)
  // While a screen is open: asked now, then every 5 s.
  createEffect(() => {
    if (!state.open) return
    ask(false)
    const timer = setInterval(() => ask(false), CLUSTERS_POLL_MS)
    ;(timer as { unref?: () => void }).unref?.()
    onCleanup(() => clearInterval(timer))
  })

  const selectedRow = () => state.clusters[Math.min(state.selected, state.clusters.length - 1)]

  const jobLines = (): JobLine[] => {
    const out: JobLine[] = []
    for (const g of state.groups) {
      const finished = g.source === "cluster" && g.finished > 0 ? ` · ${g.finished} finished recently` : ""
      out.push({ kind: "title", text: g.title + finished })
      for (const row of g.rows) out.push({ kind: "row", text: row.label, row })
      if (!g.rows.length) out.push({ kind: "empty", text: g.source === "cluster" ? "no active jobs" : "nothing running" })
    }
    if (!state.groups.length && !state.errors.length) out.push({ kind: "empty", text: "nothing runs in the background" })
    if (state.errors.length) out.push({ kind: "title", text: "could not be read" })
    for (const e of state.errors) out.push({ kind: "error", text: e })
    return out
  }

  const logout = async (row: ClusterRow) => {
    deps.say(`logging out of ${row.name}…`)
    try {
      const got = await deps.request("cluster.logout", { name: row.name })
      deps.say(!got?.ok ? `not logged out of ${row.name}: ${got?.error || got?.code || "the service said no"}`
        : got.closed === false ? `${row.name} was not logged in` : `logged out of ${row.name}`)
    } catch (error) {
      deps.say(`not logged out of ${row.name}: ${clean((error as Error)?.message ?? error, 300)}`)
    }
    refresh()
  }

  const handleKey = (key: KeyEvent): boolean => {
    if (!state.open) return false
    const plain = !key.ctrl && !key.meta
    const letter = plain ? (key.sequence ?? "").toLowerCase() : ""
    if (key.name === "escape") {
      close()
      return true
    }
    if (key.name === "up" || key.name === "down") {
      const step = key.name === "up" ? -1 : 1
      if (state.open === "jobs") {
        setState({ scroll: Math.max(0, Math.min(state.scroll + step, jobLines().length - room)) })
      } else if (state.clusters.length) {
        const n = state.clusters.length
        setState({ selected: (Math.min(state.selected, n - 1) + step + n) % n })
      }
      return true
    }
    if (letter === "r") {
      refresh()
      return true
    }
    const row = state.open === "clusters" ? selectedRow() : undefined
    if (row && (deps.keymap.matches("submit", key) || letter === "l")) {
      if (row.state === "logged_out") deps.login(row)
      else if (row.state === "logged_in") deps.say(`already logged in to ${row.name}: o logs out`)
      else deps.say(refusal(row))
      return true
    }
    if (row && letter === "o") {
      if (row.state === "logged_in") void logout(row)
      else deps.say(`${row.name} is not logged in`)
      return true
    }
    return true                           // nothing else leaves the screen
  }

  return { state, open, close, refresh, handleKey, selectedRow, jobLines,
           setRoom: (rows) => { room = Math.max(1, rows) } }
}

/** The hints line while a screen is open: what its keys do now. */
export function clustersHelp(view: ClustersView, room = Infinity): string {
  const row = view.state.open === "clusters" ? view.selectedRow() : undefined
  const parts = view.state.open === "jobs" ? ["↑↓ scroll", "r refresh", "esc back"]
    : ["↑↓ select", row?.state === "logged_out" ? "enter log in" : row?.state === "logged_in" ? "o log out" : "",
       "r refresh", "esc back"].filter(Boolean)
  const kept: string[] = []
  for (const part of parts) {
    if (width([...kept, part].join(" · ")) > room) break
    kept.push(part)
  }
  return kept.join(" · ")
}

const LOOK: Record<ClusterRow["state"], { glyph: string; word: string }> = {
  logged_in: { glyph: "●", word: "logged in" },
  logged_out: { glyph: "○", word: "logged out" },
  refused: { glyph: "✗", word: "refused" },
}

/** What the selected cluster shows under its row: a refused one's
 *  problems, another's error. */
function detailOf(row: ClusterRow): string[] {
  if (row.state === "refused") return row.problems.length ? row.problems : [refusal(row)]
  return row.error ? [row.error] : []
}

interface Line {
  kind: "title" | "row" | "detail" | "empty" | "error" | "job"
  text: string
  row?: ClusterRow
  job?: JobRow
  index?: number
}

export function ClustersPanel(props: { view: ClustersView; theme: () => Theme; width: () => number;
                                       height: () => number }): JSX.Element {
  const t = props.theme
  const v = props.view
  const inner = () => Math.max(20, props.width() - 2)
  const room = () => Math.max(1, props.height() - 1)
  const lines = (): Line[] => {
    const out: Line[] = []
    const add = (kind: Line["kind"], text: string) => out.push({ kind, text })
    if (v.state.loading) add("empty", "asking the agent service… (it looks at each cluster over ssh)")
    else if (v.state.error) add("error", `the agent service did not answer: ${v.state.error}`)
    if (v.state.open === "jobs") {
      if (!v.state.loading && !v.state.error) {
        for (const l of v.jobLines()) {
          if (l.kind === "row") out.push({ kind: "job", text: l.text, job: l.row })
          else add(l.kind, l.text)
        }
      }
      v.setRoom(room())
      const first = Math.max(0, Math.min(v.state.scroll, out.length - room()))
      return out.slice(first, first + room())
    }
    if (!v.state.loading && !v.state.error && !v.state.clusters.length) {
      add("empty", "no clusters: remote.clusters in DELFIN's settings names none")
    }
    const at = Math.min(v.state.selected, v.state.clusters.length - 1)
    v.state.clusters.forEach((row, index) => {
      out.push({ kind: "row", text: row.name, row, index })
      if (index === at) for (const text of detailOf(row)) add("detail", text)
    })
    // The selection and what it says in view.
    const sel = out.findIndex((l) => l.index === at)
    const end = sel + 1 + out.slice(sel + 1).filter((l) => l.kind === "detail").length
    let first = Math.max(0, Math.min(sel - Math.floor(room() / 3), out.length - room()))
    if (end - first > room()) first = end - room()
    return out.slice(Math.max(0, first), Math.max(0, first) + room())
  }
  const summary = () => {
    if (v.state.open === "jobs") {
      const n = v.state.groups.reduce((sum, g) => sum + g.rows.length, 0)
      return v.state.loading ? "" : [`${n} job${n === 1 ? "" : "s"}`,
                                     v.state.errors.length ? `${v.state.errors.length} not read` : ""].filter(Boolean).join(" · ")
    }
    const list = v.state.clusters
    const ins = list.filter((r) => r.state === "logged_in").length
    return v.state.loading ? "" : `${list.length} cluster${list.length === 1 ? "" : "s"}${ins ? ` · ${ins} logged in` : ""}`
  }
  const nameWidth = () => Math.min(18, Math.max(8, ...v.state.clusters.map((r) => width(r.name))))
  const tone = (state: ClusterRow["state"]) => state === "logged_in" ? t().ok : state === "refused" ? t().err : t().muted
  const stateTone = (state: string) => /fail|error|cancel|timeout|memory|kill/i.test(state) ? t().err
    : /run|complet/i.test(state) ? t().ok : /pend|queue|wait/i.test(state) ? t().warn : t().muted
  return (
    <box flexDirection="column" height={props.height()} flexShrink={0}>
      <text wrapMode="none">
        <span style={{ fg: t().accent, bold: true }}>{v.state.open === "jobs" ? "◆ JOBS  " : "◆ CLUSTERS  "}</span>
        <span style={{ fg: t().muted }}>{summary()}</span>
      </text>
      <For each={lines()}>
        {(line) => {
          if (line.kind === "title") return <text fg={t().accentSoft} wrapMode="none">{fit(` ${line.text}`, inner())}</text>
          if (line.kind === "empty") return <text fg={t().faint} wrapMode="none">{fit(`   ${line.text}`, inner())}</text>
          if (line.kind === "error") return <text fg={t().err} wrapMode="none">{fit(`   ✗ ${line.text}`, inner())}</text>
          if (line.kind === "detail") return <text fg={t().muted} wrapMode="none">{`      │ ${fit(line.text, inner() - 8)}`}</text>
          if (line.kind === "job") {
            const job = line.job!
            const kind = fit(job.kind, 10).padEnd(10)
            const room = Math.max(10, Math.floor(inner() * 0.36))
            const label = fit(job.label || job.id, room)
            const state = fit(job.state, 12)
            const detail = fit(job.detail, Math.max(0, inner() - 3 - 10 - 2 - room - 2 - 12 - 2))
            return (
              <text wrapMode="none">
                <span style={{ fg: t().muted }}>{"   " + kind + "  "}</span>
                <span style={{ fg: t().text }}>{label + " ".repeat(Math.max(0, room - width(label)))}</span>
                <span style={{ fg: stateTone(job.state) }}>{"  " + state.padEnd(12)}</span>
                <span style={{ fg: t().faint }}>{detail ? "  " + detail : ""}</span>
              </text>
            )
          }
          const row = line.row!
          const on = line.index === Math.min(v.state.selected, v.state.clusters.length - 1)
          const look = LOOK[row.state]
          const right = [budgetText(row.budget), row.jobs === null ? "" : `${row.jobs} job${row.jobs === 1 ? "" : "s"}`]
            .filter(Boolean).join("  ")
          const target = fit(row.target, Math.max(0, inner() - 5 - nameWidth() - 2 - 10 - 2 - (right ? width(right) + 2 : 0)))
          return (
            <text wrapMode="none">
              <span style={{ fg: on ? t().accent : t().faint }}>{on ? " › " : "   "}</span>
              <span style={{ fg: tone(row.state) }}>{look.glyph + " "}</span>
              <span style={{ fg: on ? t().bright : t().text, bold: on }}>{fit(row.name, nameWidth()).padEnd(nameWidth())}</span>
              <span style={{ fg: tone(row.state) }}>{"  " + look.word.padEnd(10)}</span>
              <span style={{ fg: t().muted }}>{"  " + target}</span>
              <span style={{ fg: t().faint }}>{right ? "  " + right : ""}</span>
            </text>
          )
        }}
      </For>
    </box>
  )
}
