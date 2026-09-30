module.exports = {
  rootDir: __dirname,
  testEnvironment: "node",
  transform: { "^.+\\.[jt]sx?$": ["babel-jest", { presets: [["babel-preset-react-app", { runtime: "automatic" }]] }] },
  transformIgnorePatterns: ["node_modules/(?!axios)/"],
  moduleNameMapper: { "^@/(.*)$": "<rootDir>/src/$1" },
  testMatch: ["<rootDir>/src/lib/__tests__/**/*.test.js"],
};
