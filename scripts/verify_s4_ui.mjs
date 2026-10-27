/** CUA browser regression. Pass an authenticated local Agent Studio Tab handle.
 * Only configuration versions are appended; no tasks, approvals or model calls.
 * Starts/ends on the original executable active version; keeps the imported draft.
 */
import assert from 'node:assert/strict'

export async function verifyS4Ui(tab) {
  assert.match(await tab.url(), /^http:\/\/127\.0\.0\.1:5184\/agent-studio$/)
  const versionBox = () => tab.playwright.getByRole('combobox', { name: '配置版本', exact: true })
  await versionBox().click()
  let state = await tab.playwright.domSnapshot()
  const active = state.match(/- option "(v\d+ \(当前激活\))"/)
  assert.ok(active, 'An executable version must already be active')
  const originalVersion = active[1].match(/^v\d+/)[0]
  await tab.playwright.getByRole('option', { name: active[1], exact: true }).click()
  await tab.playwright.domSnapshot()
  await tab.playwright.getByRole('button', { name: '配置与评测', exact: true }).click()
  await tab.playwright.domSnapshot()
  await tab.playwright.getByRole('button', { name: '导入配置', exact: true }).click()
  await tab.playwright.domSnapshot()
  const area = tab.playwright.getByTestId('import-dsl-textarea')
  const dsl = JSON.parse(await area.evaluate(el => el.value))
  const retriever = dsl.workflow.graph.nodes.find(n => n.node_id === 'node_mcp_retriever')
  retriever.parameters.top_k = retriever.parameters.top_k === 7 ? 6 : 7
  dsl.workflow.graph.nodes.find(n => n.node_id === 'node_dimension_analyst').system_prompt +=
    '\n只依据所提供证据作答，缺少依据时列明缺口。'
  await area.fill(JSON.stringify(dsl, null, 2))
  await tab.playwright.getByPlaceholder('填写本次导入维护说明').fill('S4 CUA 回归：显式激活后回滚；无模型调用')
  await tab.playwright.getByTestId('btn-confirm-import-workflow').click()
  await tab.playwright.getByTestId('btn-confirm-import-workflow').waitFor({ state: 'hidden' })
  await tab.playwright.getByTestId('btn-activate-workflow').waitFor({ state: 'visible' })
  state = await tab.playwright.domSnapshot()
  assert.match(state, /button "激活此版本"/)
  assert.doesNotMatch(state, /generic: v\d+ \(当前激活\)/)
  await tab.playwright.getByTestId('btn-activate-workflow').click()
  await tab.playwright.getByTestId('btn-activate-workflow').waitFor({ state: 'hidden' })
  state = await tab.playwright.domSnapshot()
  const imported = state.match(/generic: (v\d+) \(当前激活\)/)
  assert.ok(imported)
  assert.notEqual(imported[1], originalVersion)
  await versionBox().click()
  await tab.playwright.domSnapshot()
  await tab.playwright.getByRole('option', { name: originalVersion, exact: true }).click()
  state = await tab.playwright.domSnapshot()
  assert.match(state, /button "回滚到此版本"/)
  await tab.playwright.getByRole('button', { name: '回滚到此版本', exact: true }).click()
  await tab.playwright.getByTestId('btn-activate-workflow').waitFor({ state: 'hidden' })
  state = await tab.playwright.domSnapshot()
  assert.ok(state.includes(`generic: ${originalVersion} (当前激活)`))
  return { passed: true, original_version: originalVersion, imported_version: imported[1],
    checks: ['import inactive', 'explicit activation', 'rollback'], model_calls: 0, approvals: 0 }
}
