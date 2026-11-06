import { expect, test } from "../fixtures/seed";

test("owned fixture exposes real setup and capabilities through inference control", async ({ page, seed }) => {
  const data = await seed.owned();
  const headers = { Authorization: `Bearer ${await seed.accessToken(data.admin_email)}` };
  const path = `/api/v1/projects/${data.project_id}/ml-backends/${data.ml_backend_id}`;

  const setupResponse = await page.request.get(`${path}/setup`, { headers });
  expect(setupResponse.ok(), await setupResponse.text()).toBe(true);
  const setup = await setupResponse.json();
  expect(setup.models).toEqual(expect.arrayContaining([
    expect.objectContaining({ id: "e2-detect", task: "detection" }),
    expect.objectContaining({ id: "e2-interactive", task: "segmentation" }),
  ]));

  const capabilitiesResponse = await page.request.get(`${path}/capabilities`, { headers });
  expect(capabilitiesResponse.ok(), await capabilitiesResponse.text()).toBe(true);
  const capabilities = await capabilitiesResponse.json();
  expect(capabilities.models).toEqual(expect.arrayContaining([
    expect.objectContaining({ id: "e2-detect", task: "detection" }),
    expect.objectContaining({ id: "e2-interactive", task: "segmentation" }),
  ]));
});
