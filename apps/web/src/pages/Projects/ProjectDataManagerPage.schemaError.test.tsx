import { screen } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { expectNoUnexpectedApiRequests, resetUnexpectedApiRequests } from "@/test/apiRequestGuard";
import { createTestUser, resetAuthUser, seedAuthUser } from "@/test/auth";
import { installDataManagerApi } from "@/test/dataManagerApi";
import { renderWithProviders } from "@/test/renderWithProviders";

import { ProjectDataManagerPage } from "./ProjectDataManagerPage";

beforeEach(() => {
  resetUnexpectedApiRequests();
  seedAuthUser(createTestUser());
});

afterEach(() => {
  expectNoUnexpectedApiRequests();
  resetAuthUser();
});

describe("ProjectDataManagerPage schema failure", () => {
  it("shows the retry action without refetching the schema in a loop", async () => {
    const captured = installDataManagerApi({ schemaStatus: 500 });

    renderWithProviders(
      <Routes>
        <Route path="/projects/:id/data-manager" element={<ProjectDataManagerPage />} />
      </Routes>,
      { initialEntries: ["/projects/p1/data-manager?lens=tasks"] },
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("无法加载 Data Manager 筛选字段");

    // The error branch must stay mounted with its explicit retry action instead
    // of remounting an observer that retries the errored schema query forever.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(captured.schemaRequests).toBe(1);
    expect(captured.taskQueries).toHaveLength(0);
  });
});
