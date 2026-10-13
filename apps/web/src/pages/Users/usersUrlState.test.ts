import { describe, expect, it } from "vitest";
import {
  INVITATION_URL_DEFAULTS,
  USERS_URL_DEFAULTS,
  invitationUrlCodec,
  parseInvitationUrl,
  parseUsersUrl,
  usersUrlCodec,
} from "./usersUrlState";

describe("Users URL state", () => {
  it("accepts explicit default enum values without reporting decode issues", () => {
    expect(parseUsersUrl("?tab=members&status=active").issues).toEqual([]);
  });

  it("round-trips member filters while preserving unrelated links", () => {
    const current = new URLSearchParams("focus=u1&invite_q=kept");
    const encoded = usersUrlCodec.encode(current, {
      ...USERS_URL_DEFAULTS,
      tab: "members",
      q: " Alice ",
      status: "inactive",
      role: "employee",
      projectId: "p1",
      groupId: "g1",
      page: 2,
    });
    expect(encoded.get("focus")).toBe("u1");
    expect(encoded.get("invite_q")).toBe("kept");
    expect(encoded.get("q")).toBe("Alice");
    expect(parseUsersUrl(encoded).state).toMatchObject({
      q: "Alice",
      status: "inactive",
      role: "employee",
      projectId: "p1",
      groupId: "g1",
      page: 2,
    });
  });

  it("uses safe defaults for invalid status, tab, and page", () => {
    const parsed = parseUsersUrl("?tab=broken&status=broken&page=0");
    expect(parsed.state).toEqual(USERS_URL_DEFAULTS);
    expect(parsed.issues.map((item) => item.key)).toEqual(["tab", "status", "page"]);
  });
});

describe("Invitation URL state", () => {
  it("accepts explicit all/me defaults without reporting decode issues", () => {
    expect(parseInvitationUrl("?invite_status=all&invite_scope=me").issues).toEqual([]);
  });

  it("omits all/me defaults and uses an independent namespace", () => {
    const encoded = invitationUrlCodec.encode(
      new URLSearchParams("q=member&status=active&focus=u1"),
      INVITATION_URL_DEFAULTS,
    );
    expect(encoded.toString()).toBe("q=member&status=active&focus=u1");
    expect(
      parseInvitationUrl("?invite_status=pending&invite_scope=all&invite_page=2").state,
    ).toEqual({
      q: "",
      status: "pending",
      scope: "all",
      role: "",
      projectId: "",
      page: 2,
    });
  });
});
