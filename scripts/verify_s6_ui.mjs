/** Run through cua_repl with an authenticated CUA Tab on the isolated synthetic task.
 * No standalone Playwright/CDP driver, hidden app state or provider API is used.
 * await verifyS6Round(tab, { round: 1, answerOption: <visible option text> });
 */
export async function verifyS6Round(tab, { round, answerOption }) {
  const pause = tab.playwright.getByTestId('analysis-user-fact-pause');
  await pause.waitFor({ state: 'visible', timeoutMs: 20000 });
  const label = await pause.innerText();
  if (!label.includes(`第 ${round}/2 轮`)) throw new Error('Unexpected question round');
  await tab.playwright.getByRole('button', { name: answerOption, exact: true }).click();
  const input = tab.playwright.getByPlaceholder('请回答分析中的业务追问；不确定可以如实说明');
  if (await input.evaluate(el => el.value) !== answerOption) throw new Error('Chip did not fill answer');
  if (!(await pause.isVisible())) throw new Error('Example was sent before confirmation');
  await tab.playwright.getByRole('button', { name: '发送', exact: true }).click();
  return { round, optionFilledWithoutAutoSubmit: true, answerSubmitted: true };
}

export async function verifyS6Completed(tab) {
  await tab.playwright.getByRole('button', { name: /协同过程/ }).waitFor({state:'visible',timeoutMs:20000});
  if (await tab.playwright.getByTestId('analysis-user-fact-pause').isVisible()) {
    throw new Error('Completed task still shows active questions');
  }
  if ((await tab.playwright.getByRole('button', { name: /^D[1-8]/ }).count()) !== 8) {
    throw new Error('Missing report dimension');
  }
  return { reportShown: true, dimensions: 8, syntheticFixture: true };
}
