/**
 * /clusters, /jobs and the masked login field, in the window: the screens
 * ask the agent service (clusters.list, jobs.list) and say what it
 * answered -- three states of a cluster, groups of background work, the
 * sources it could not read. Enter on a logged-out cluster opens the
 * login field, whose text is drawn as • and goes out in exactly one
 * cluster.login: never into the prompt, the input history, a session's
 * prompt, a question's answer or an attention item's answer, nor onto the
 * screen. A login item of NEEDS YOU opens the same field. Rendered
 * headless through the real App against a fake agent service.
 */
import { afterEach, describe, expect, test } from "bun:test"
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { attentionCounts, markOf } from "../../src/agents/attention"
import { clusterRows, jobGroups, refusal } from "../../src/clusters/view"
import { History } from "../../src/composer/history"
import type { AttentionItem } from "../../src/protocol/generated"
import { emptyState } from "../../src/store/state"
import { mountWindow, type Call } from "../harness"

let w: Awaited<ReturnType<typeof mountWindow>> | null = null
const dirs: string[] = []
afterEach(async () => {
  await w?.close()
  w = null
  for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true })
})

const SECRET = "s3cret-Zq9"
const OTP = "481516"

const cluster = (extra: Record<string, unknown>) => ({
  name: "kcist", target: "jonas@kcist.example.org", state: "logged_out", problems: [], requiresPassword: true,
  requiresOtp: false, workDir: "~/delfin", defaultPartition: "cpu", budget: null, jobs: null, error: null, ...extra,
})
const CLUSTERS = [
  cluster({}),
  cluster({ name: "justus", target: "jonas@justus2.uni-ulm.de", state: "logged_in", requiresOtp: true,
            budget: { used: 120, limit: 500, unit: "h" }, jobs: 3 }),
  cluster({ name: "horeka", target: "me@horeka.example", state: "refused",
            problems: ["ctun has not pinned its host key; check the fingerprint and run `ctun -t horeka hostkey --pin`",
                       "work_dir '~x' must be a directory below ~"] }),
]
const JOBS = {
  groups: [
    { source: "agent", title: "optimise benzene", rows: [
      { id: "bg-1", kind: "bash", label: "orca opt.inp", state: "", detail: "started 4m ago" }] },
    { source: "monitor", title: "DELFIN's job monitor", rows: [
      { id: "4711", kind: "SLURM", label: "~/calc/benzene", state: "RUNNING", detail: "watched by DELFIN's job monitor" }] },
    { source: "cluster", cluster: "justus", title: "justus (jonas@justus2.uni-ulm.de)", finished: 2, rows: [
      { id: "99001", kind: "cpu", label: "scan_01", state: "PENDING", detail: "0:00 of 2:00:00 · 1 node(s)" }] },
  ],
  errors: ["kcist: tunnel_down: the control socket does not answer"],
}

const loginItem: AttentionItem = {
  id: "att-l", kind: "login", group: "needs", blocking: false, name: "log in to kcist",
  text: "a cluster tool needs a login to kcist", detail: "A cluster tool needs a login to kcist.", workspace: "/w",
  host: "", sessionId: "", sessionTitle: "", sessionOpen: false, sessionSaved: false, createdAt: Date.now() / 1000 - 60,
  options: [], job: null, cluster: "kcist",
}

async function mount(options: { version?: number; clusters?: Record<string, unknown>[];
                                login?: (params: Record<string, any>) => unknown } = {}) {
  const calls: Call[] = []
  const dir = mkdtempSync(join(tmpdir(), "delfin-clusters-"))
  dirs.push(dir)
  const historyFile = join(dir, "tui_history.jsonl")
  const history = new History(historyFile)
  const client = {
    request: async (method: string, params: Record<string, any> = {}) => {
      calls.push({ method, params })
      if (method === "clusters.list") return { clusters: options.clusters ?? CLUSTERS }
      if (method === "jobs.list") return JOBS
      if (method === "cluster.login") return options.login?.(params) ?? { ok: true, opened: true, code: null, error: null }
      if (method === "cluster.logout") return { ok: true, closed: true, code: null, error: null }
      if (method === "sessions.list") return { sessions: [{ sessionId: "s1", state: "idle" }], elsewhere: [] }
      if (method === "session.replay") return { events: [] }
      if (method === "attention.answer" || method === "attention.dismiss") return { ok: true }
      return { accepted: true, applied: true, message: "" }
    },
  }
  w = await mountWindow({ width: 110, height: 30, full: true, client, history })
  w.store.update((d) => { d.serviceVersion = options.version ?? 4 })
  await w.settle()
  return { w, calls, history, historyFile }
}

const screen = () => w!.screenLines().join("\n")
const called = (calls: Call[], method: string) => calls.filter((c) => c.method === method).map((c) => c.params)
/** A line typed and sent, as a person does (esc closes the completion list). */
const line = async (text: string) => {
  await w!.keys.typeText(text)
  w!.keys.pressEscape()
  await Bun.sleep(40)
  w!.keys.pressEnter()
  await w!.settle()
}
/** Until the fake service's answers are drawn. */
const settled = async () => {
  for (let i = 0; i < 3; i++) await w!.settle()
}
const type = async (text: string) => {
  await w!.keys.typeText(text)
  await w!.settle()
}
/** What the window shows and keeps anywhere a test can look: the screen,
 *  its colours' text, the store, the history and every request but the
 *  one login. */
const everywhere = (calls: Call[], history: History) => [
  screen(), w!.footer().join("\n"), JSON.stringify(w!.t.captureSpans()), JSON.stringify(w!.store.state),
  JSON.stringify(history.entries), JSON.stringify(calls.filter((c) => c.method !== "cluster.login")),
].join("\n")

describe("reading what the service sends", () => {
  test("a cluster's row and a group of jobs, each field as its type says", () => {
    const rows = clusterRows([...CLUSTERS, { state: "logged_in" }, null, cluster({ name: "x", state: "weird",
                                                                                     target: "a\u001b[31mb" })])
    expect(rows.map((r) => [r.name, r.state])).toEqual([["kcist", "logged_out"], ["justus", "logged_in"],
                                                        ["horeka", "refused"], ["x", "refused"]])
    expect(rows[1]).toMatchObject({ budget: { used: 120, limit: 500, unit: "h" }, jobs: 3, requiresOtp: true })
    expect(rows[3]!.target).toBe("a [31mb")                      // no escape reaches the terminal
    const groups = jobGroups(JOBS.groups)
    expect(groups.map((g) => [g.source, g.title, g.rows.length, g.finished])).toEqual([
      ["agent", "optimise benzene", 1, 0], ["monitor", "DELFIN's job monitor", 1, 0],
      ["cluster", "justus (jonas@justus2.uni-ulm.de)", 1, 2]])
    expect(refusal(rows[2]!)).toBe("horeka cannot be logged into: ctun has not pinned its host key; check the "
      + "fingerprint and run `ctun -t horeka hostkey --pin`")
    expect(refusal({ ...rows[2]!, problems: ["the host key changed"] }))
      .toBe("horeka cannot be logged into: the host key changed (ctun -t horeka hostkey --pin)")
  })

  test("a login item waits for the user: marked and counted as one that asks", () => {
    expect(markOf(loginItem)).toBe("asks")
    const state = emptyState()
    state.attention = [loginItem]
    expect(attentionCounts(state, 0)).toEqual({ asks: 1, failed: 0 })
  })
})

describe("/clusters", () => {
  test("asks for the clusters and shows the three states; enter on a logged-out one opens the login field", async () => {
    const { calls } = await mount()
    await line("/clusters")
    await settled()
    expect(called(calls, "clusters.list")).toHaveLength(1)
    const text = screen()
    expect(text).toContain("◆ CLUSTERS  3 clusters · 1 logged in")
    expect(text).toMatch(/› ○ kcist\s+logged out\s+jonas@kcist\.example\.org/)
    expect(text).toMatch(/● justus\s+logged in\s+jonas@justus2\.uni-ulm\.de\s+120\/500 h  3 jobs/)
    expect(text).toMatch(/✗ horeka\s+refused\s+me@horeka\.example/)
    expect(text).toContain("↑↓ select · enter log in · r refresh · esc back")
    expect(called(calls, "session.prompt")).toEqual([])
    w!.keys.pressEnter()
    await w!.settle()
    expect(screen()).toContain("log in to kcist")
    expect(screen()).toMatch(/› password\s+▏/)
    expect(screen()).toContain("enter logs in · ctrl+u clears · esc cancels")
    w!.keys.pressEnter()                                             // empty: no attempt spent
    await settled()
    expect(screen()).toContain("the password is empty")
    expect(called(calls, "cluster.login")).toEqual([])
  })

  test("the selected row says what is wrong; a refused one cannot be logged into and says why", async () => {
    const { calls } = await mount()
    await line("/clusters")
    await settled()
    w!.keys.pressArrow("up")                                         // around to the last: horeka
    await w!.settle()
    let text = screen()
    expect(text).toContain("│ ctun has not pinned its host key; check the fingerprint and run `ctun -t horeka hostkey --pin`")
    expect(text).toContain("│ work_dir '~x' must be a directory below ~")
    w!.keys.pressEnter()
    await w!.settle()
    text = screen()
    expect(text).toContain("horeka cannot be logged into: ctun has not pinned its host key")
    expect(text).not.toContain("log in to horeka")
    expect(called(calls, "cluster.login")).toEqual([])
  })

  test("o logs out of a logged-in cluster and says so; r asks again; esc goes back", async () => {
    const { calls } = await mount()
    await line("/clusters")
    await settled()
    w!.keys.pressArrow("down")
    await w!.settle()
    expect(screen()).toContain("↑↓ select · o log out · r refresh · esc back")
    await type("o")
    await settled()
    expect(called(calls, "cluster.logout")).toEqual([{ name: "justus" }])
    expect(screen()).toContain("logged out of justus")
    const before = called(calls, "clusters.list").length
    await type("r")
    await settled()
    expect(called(calls, "clusters.list").length).toBeGreaterThan(before)
    await w!.escape()
    expect(screen()).not.toContain("◆ CLUSTERS")
    expect(called(calls, "session.prompt")).toEqual([])
  })

  test("with a service before version 4 it says so in a line and asks nothing", async () => {
    const { calls } = await mount({ version: 3 })
    await line("/clusters")
    await settled()
    expect(screen()).toContain("the agent service is too old for /clusters; restart it")
    expect(screen()).not.toContain("◆ CLUSTERS")
    expect(called(calls, "clusters.list")).toEqual([])
    expect(called(calls, "session.prompt")).toEqual([])
    await line("/jobs")
    await settled()
    expect(screen()).toContain("the agent service is too old for /jobs; restart it")
    expect(called(calls, "jobs.list")).toEqual([])
  })
})

describe("the login field", () => {
  test("draws only •, and sends the password in exactly one cluster.login -- nowhere else", async () => {
    const { calls, history, historyFile } = await mount({
      login: () => new Promise((resolve) => setTimeout(() => resolve({ ok: true, opened: true }), 150)),
    })
    await line("/clusters")
    await settled()
    w!.keys.pressEnter()
    await w!.settle()
    await type(SECRET)
    let text = screen()
    expect(text).toMatch(new RegExp(`› password\\s+${"•".repeat(SECRET.length)}▏`))
    expect(everywhere(calls, history)).not.toContain(SECRET)
    w!.keys.pressEnter()
    await w!.settle()
    expect(screen()).toContain("logging in…")
    expect(screen()).not.toContain("•")                               // cleared as it went out
    expect(called(calls, "cluster.login")).toEqual([{ name: "kcist", password: SECRET }])
    await Bun.sleep(200)
    await settled()
    text = screen()
    expect(text).not.toContain("log in to kcist")                    // closed
    expect(text).toContain("logged in to kcist")
    expect(called(calls, "cluster.login")).toHaveLength(1)
    // Nothing of it anywhere else: no prompt, reply or answer was sent,
    // and the history has the command only.
    for (const method of ["session.prompt", "permission.reply", "attention.answer", "session.message"]) {
      expect(called(calls, method)).toEqual([])
    }
    expect(history.entries.map((e) => e.text)).toEqual(["/clusters"])
    expect(existsSync(historyFile) ? readFileSync(historyFile, "utf8") : "").not.toContain(SECRET)
    expect(everywhere(calls, history)).not.toContain(SECRET)
    expect(w!.store.state.sessions.s1!.pending).toEqual([])
  })

  test("a failure says why, and the field is empty again; the next try that works closes it", async () => {
    let tries = 0
    const { calls } = await mount({
      login: () => ++tries === 1 ? { ok: false, opened: false, code: "login_failed", error: "the password or the OTP was not accepted" }
        : { ok: true, opened: true },
    })
    await line("/clusters")
    await settled()
    w!.keys.pressEnter()
    await w!.settle()
    await type("wrong-one")
    w!.keys.pressEnter()
    await settled()
    let text = screen()
    expect(text).toContain("the password or the OTP was not accepted")
    expect(text).toContain("log in to kcist")                        // still open
    expect(text).toMatch(/› password\s+▏/)                           // empty, and focused
    await type(SECRET)
    w!.keys.pressEnter()
    await settled()
    text = screen()
    expect(text).not.toContain("log in to kcist")
    expect(text).toContain("logged in to kcist")
    expect(called(calls, "cluster.login")).toEqual([{ name: "kcist", password: "wrong-one" },
                                                   { name: "kcist", password: SECRET }])
    // Asked again: the screen shows the cluster logged in once the service says so.
    expect(called(calls, "clusters.list").length).toBeGreaterThanOrEqual(2)
  })

  test("a failure that only has a code shows the code", async () => {
    await mount({ login: () => ({ ok: false, opened: false, code: "cluster_not_usable", error: null }) })
    await line("/clusters")
    await settled()
    w!.keys.pressEnter()
    await w!.settle()
    await type("x")
    w!.keys.pressEnter()
    await settled()
    expect(screen()).toContain("cluster_not_usable")
  })

  test("a paste goes into the field, not the prompt; backspace and ctrl+u edit it; esc sends nothing", async () => {
    const { calls, history } = await mount()
    await line("/clusters")
    await settled()
    w!.keys.pressEnter()
    await w!.settle()
    await w!.keys.pasteBracketedText(`${SECRET}\n`)
    await w!.settle()
    expect(screen()).toMatch(new RegExp(`› password\\s+${"•".repeat(SECRET.length)}▏`))
    expect(screen()).not.toContain(SECRET)
    expect(screen()).toContain("❯ the keys are the screen's: esc goes back")     // the prompt is empty
    w!.keys.pressBackspace()
    await w!.settle()
    expect(screen()).toMatch(new RegExp(`› password\\s+${"•".repeat(SECRET.length - 1)}▏`))
    w!.keys.pressKey("u", { ctrl: true })
    await w!.settle()
    expect(screen()).toMatch(/› password\s+▏/)
    // One the prompt would fold into a placeholder: the field's too.
    await w!.keys.pasteBracketedText(Array.from({ length: 12 }, (_, i) => `${SECRET}-${i}`).join("\n"))
    await w!.settle()
    expect(screen()).not.toContain("[Pasted text")
    expect(screen()).toContain("❯ the keys are the screen's: esc goes back")
    w!.keys.pressKey("u", { ctrl: true })
    await type(SECRET)
    await w!.escape()
    expect(screen()).not.toContain("log in to kcist")
    expect(screen()).toContain("◆ CLUSTERS")                         // back on the screen
    expect(called(calls, "cluster.login")).toEqual([])
    expect(everywhere(calls, history)).not.toContain(SECRET)
    // And after it: keys are the screen's again, the prompt still empty.
    await w!.escape()
    expect(screen()).toContain("❯ ask DELFIN…")
  })

  test("with a question of the session open, no key typed into the field answers it", async () => {
    const { calls, history } = await mount()
    w!.store.push({ type: "permission.asked", session: "s1", ts: 0, seq: 1, requestId: "r1", kind: "confirm", tool: "bash",
                    preview: "$ sbatch scan.sh", flags: { shell: true }, options: [{ key: "y", label: "yes" },
                    { key: "n", label: "no" }, { key: "A", label: "always this exact command" }] } as never)
    await w!.settle()
    await line("/clusters")                                          // an approval takes no line: it is the window's
    await settled()
    w!.keys.pressEnter()
    await w!.settle()
    await type(`yA${SECRET}`)                                        // the question's keys among them
    w!.keys.pressEnter()
    await settled()
    expect(w!.replies()).toEqual([])
    expect(w!.store.state.sessions.s1!.pending.map((p) => p.requestId)).toEqual(["r1"])
    expect(called(calls, "cluster.login")).toEqual([{ name: "kcist", password: `yA${SECRET}` }])
    expect(everywhere(calls, history)).not.toContain(SECRET)
  })

  test("a cluster that asks for an OTP too shows both fields and sends both", async () => {
    const { calls, history } = await mount({ clusters: [cluster({ requiresOtp: true })] })
    await line("/clusters")
    await settled()
    w!.keys.pressEnter()
    await w!.settle()
    let text = screen()
    expect(text).toMatch(/› password\s+▏/)
    expect(text).toMatch(/ {2}OTP/)
    expect(text).toContain("enter next field · tab other field · ctrl+u clears · esc cancels")
    await type(SECRET)
    w!.keys.pressEnter()                                             // to the OTP
    await w!.settle()
    text = screen()
    expect(text).toMatch(new RegExp(` {2}password\\s+${"•".repeat(SECRET.length)}\\s`))
    expect(text).toMatch(/› OTP\s+▏/)
    expect(called(calls, "cluster.login")).toEqual([])
    await type(OTP)
    w!.keys.pressTab()                                               // around, and back
    w!.keys.pressTab()
    await w!.settle()
    expect(screen()).toMatch(new RegExp(`› OTP\\s+${"•".repeat(OTP.length)}▏`))
    w!.keys.pressEnter()
    await settled()
    expect(called(calls, "cluster.login")).toEqual([{ name: "kcist", password: SECRET, otp: OTP }])
    expect(everywhere(calls, history)).not.toContain(SECRET)
    expect(everywhere(calls, history)).not.toContain(OTP)
  })
})

describe("/jobs", () => {
  test("asks for the jobs and shows each group's title, its rows, and what could not be read", async () => {
    const { calls } = await mount()
    await line("/jobs")
    await settled()
    expect(called(calls, "jobs.list")).toHaveLength(1)
    expect(called(calls, "session.prompt")).toEqual([])
    const text = screen()
    expect(text).toContain("◆ JOBS  3 jobs · 1 not read")
    expect(text).toMatch(/ optimise benzene\s*\n\s+bash\s+orca opt\.inp\s+started 4m ago/)
    expect(text).toMatch(/ DELFIN's job monitor\s*\n\s+SLURM\s+~\/calc\/benzene\s+RUNNING\s+watched by DELFIN's job monitor/)
    expect(text).toMatch(/ justus \(jonas@justus2\.uni-ulm\.de\) · 2 finished recently\s*\n\s+cpu\s+scan_01\s+PENDING\s+0:00 of 2:00:00/)
    expect(text).toMatch(/ could not be read\s*\n\s+✗ kcist: tunnel_down: the control socket does not answer/)
    expect(text.indexOf("optimise benzene")).toBeLessThan(text.indexOf("could not be read"))
    expect(text).toContain("↑↓ scroll · r refresh · esc back")
    await w!.escape()
    expect(screen()).not.toContain("◆ JOBS")
  })

  test("/jobs with an argument is the session's, as typed", async () => {
    const { calls } = await mount()
    await line("/jobs watch on")
    expect(called(calls, "session.prompt").map((p) => p.text)).toEqual(["/jobs watch on"])
    expect(called(calls, "jobs.list")).toEqual([])
  })
})

describe("a login item of NEEDS YOU", () => {
  const items = async () => {
    w!.store.push({ type: "attention.updated", session: "", ts: 0, items: [loginItem] } as never)
    await w!.settle()
  }
  const peekLogin = async () => {
    w!.keys.pressArrow("left")
    await w!.settle()
    for (let i = 0; i < 6 && !screen().split("\n").some((l) => l.startsWith(" › ") && l.includes("log in to kcist")); i++) {
      w!.keys.pressArrow("down")
      await w!.settle()
    }
    w!.keys.pressKey(" ")
    await w!.settle()
  }

  test("enter opens the login field for its cluster over the view; it is never answered", async () => {
    const { calls, history } = await mount()
    await items()
    await peekLogin()
    let text = screen()
    expect(text).toContain("◉ log in to kcist")
    expect(text).toContain("│ → opens the masked login field for kcist")
    expect(text).toContain("type to log in to kcist (a masked field)…")
    w!.keys.pressEnter()
    await settled()
    text = screen()
    expect(called(calls, "clusters.list")).toHaveLength(1)
    expect(text).toContain("log in to kcist")
    expect(text).toContain("jonas@kcist.example.org")
    expect(text).toContain("◆ AGENTS")                               // the view is still there
    await type(SECRET)
    w!.keys.pressEnter()
    await settled()
    expect(called(calls, "cluster.login")).toEqual([{ name: "kcist", password: SECRET }])
    expect(called(calls, "attention.answer")).toEqual([])
    expect(called(calls, "session.create")).toEqual([])
    expect(screen()).toContain("logged in to kcist")
    // The service closes the item: attention.updated drops it.
    w!.store.push({ type: "attention.updated", session: "", ts: 0, items: [] } as never)
    await w!.settle()
    expect(screen()).not.toContain("◉ log in to kcist")
    expect(everywhere(calls, history)).not.toContain(SECRET)
  })

  test("typing on its peek goes into the field, from the first key -- never the prompt", async () => {
    const { calls, history } = await mount()
    await items()
    await peekLogin()
    await type(SECRET)
    await settled()
    expect(screen()).toMatch(new RegExp(`› password\\s+${"•".repeat(SECRET.length)}▏`))
    expect(screen()).toContain("❯ type to log in to kcist (a masked field)…")    // the prompt is empty
    w!.keys.pressEnter()
    await settled()
    expect(called(calls, "cluster.login")).toEqual([{ name: "kcist", password: SECRET }])
    expect(called(calls, "attention.answer")).toEqual([])
    expect(everywhere(calls, history)).not.toContain(SECRET)
    expect(history.entries).toEqual([])
  })

  test("ctrl+x dismisses it (twice: it waits for the user), and opens nothing", async () => {
    const { calls } = await mount()
    await items()
    await peekLogin()
    w!.keys.pressKey("x", { ctrl: true })
    await w!.settle()
    expect(called(calls, "attention.dismiss")).toEqual([])
    w!.keys.pressKey("x", { ctrl: true })
    await settled()
    expect(called(calls, "attention.dismiss")).toEqual([{ id: "att-l" }])
    expect(screen()).toContain("dismissed: log in to kcist")
    expect(called(calls, "clusters.list")).toEqual([])
    expect(called(calls, "attention.answer")).toEqual([])
  })

  test("a cluster the service does not know is said, and nothing is sent", async () => {
    const { calls } = await mount({ clusters: [cluster({ name: "justus" })] })
    await items()
    await peekLogin()
    w!.keys.pressArrow("right")
    await settled()
    expect(screen()).toContain("kcist is not one of DELFIN's clusters")
    expect(screen()).not.toContain("╭ log in to kcist")
    expect(called(calls, "cluster.login")).toEqual([])
    expect(called(calls, "attention.answer")).toEqual([])
  })
})
