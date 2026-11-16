import next from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const config = [
  ...next,
  ...nextTs,
  {
    rules: {
      // All copy, lead text and brand text is untrusted: React escapes it; raw HTML is never set.
      "react/no-danger": "error",
      "no-restricted-syntax": [
        "error",
        { selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']", message: "Never render raw HTML." },
        { selector: "JSXAttribute[name.name='style']", message: "Inline styles break the CSP; use a class." },
      ],
    },
  },
  { ignores: [".next/**", "node_modules/**", "next-env.d.ts", "public/sw.js"] },
];

export default config;
