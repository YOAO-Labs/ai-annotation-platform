export const PASSWORD_MIN_LENGTH = 8;
export const PASSWORD_MAX_LENGTH = 128;

export interface PasswordRequirement {
  key: "length" | "maxLength" | "uppercase" | "lowercase" | "number";
  label: string;
  ok: boolean;
}

export function getPasswordRequirements(password: string): PasswordRequirement[] {
  const codePointLength = Array.from(password).length;
  return [
    {
      key: "length",
      label: `至少 ${PASSWORD_MIN_LENGTH} 位`,
      ok: codePointLength >= PASSWORD_MIN_LENGTH,
    },
    {
      key: "maxLength",
      label: `不超过 ${PASSWORD_MAX_LENGTH} 位`,
      ok: codePointLength <= PASSWORD_MAX_LENGTH,
    },
    { key: "uppercase", label: "含大写字母", ok: /[A-Z]/.test(password) },
    { key: "lowercase", label: "含小写字母", ok: /[a-z]/.test(password) },
    { key: "number", label: "含数字", ok: /\p{Decimal_Number}/u.test(password) },
  ];
}

export function isPasswordStrong(password: string): boolean {
  return getPasswordRequirements(password).every((rule) => rule.ok);
}

export function getPasswordValidationErrors(password: string): string[] {
  return getPasswordRequirements(password)
    .filter((rule) => !rule.ok)
    .map((rule) => rule.label);
}
