/** CUA E2E: pass an authenticated S5 Agent Studio tab. Read-only, no model calls/approvals. */
import assert from 'node:assert/strict'

export async function verifyS5Ui(tab, baseUrl = 'http://127.0.0.1:5185') {
  assert.equal(await tab.url(), baseUrl + '/agent-studio')
  if (!await tab.playwright.getByRole('button', { name: '质量评测', exact: true }).isVisible()) {
    await tab.playwright.getByRole('button', { name: '配置与评测', exact: true }).click()
  }
  await tab.playwright.domSnapshot()
  await tab.playwright.getByRole('button', { name: '质量评测', exact: true }).waitFor({ state: 'visible' })
  await tab.playwright.getByRole('button', { name: '质量评测', exact: true }).click()
  await tab.playwright.getByRole('dialog', { name: '分析质量评测 · 五国 / 八维度' }).waitFor({ state: 'visible' })
  const snapshot = await tab.playwright.domSnapshot()
  assert.match(snapshot, /Recall@5/)
  assert.match(snapshot, /引文忠实度/)
  assert.match(snapshot, /llm_judged/)
  assert.match(snapshot, /Judge覆盖/)
  assert.match(snapshot, /0\/32 未经人工复核/)
  assert.match(snapshot, /已完成 32\/32 题/)
  assert.match(snapshot, /题有分析项/)
  assert.doesNotMatch(snapshot, /10 道|H01 - H10|100%.*法律准确率/)
  await tab.playwright.getByRole('button', { name: '关闭此对话框', exact: true }).click()
  await tab.playwright.getByRole('dialog', { name: '分析质量评测 · 五国 / 八维度' }).waitFor({ state: 'hidden' })
  await tab.playwright.domSnapshot()
  await tab.playwright.getByRole('button', { name: '查看环节产出', exact: true }).click()
  await tab.playwright.getByTestId('stage-deliverables-drawer').waitFor({ state: 'visible' })
  const trace = await tab.playwright.domSnapshot()
  assert.match(trace, /本次任务执行版本 v\d+/)
  assert.match(trace, /input|输入|环节|交接/)
  return { passed: true, human_reviews_changed: 0, new_model_requests: 0 }
}
