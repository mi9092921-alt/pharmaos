// Enforces the commit convention defined in CLAUDE.md.
// Types include the project-specific "security" and "compliance" scopes.
// Subject-case rules are disabled so Arabic commit descriptions are accepted.
export default {
  extends: ['@commitlint/config-conventional'],
  rules: {
    'type-enum': [
      2,
      'always',
      [
        'feat',
        'fix',
        'perf',
        'security',
        'compliance',
        'docs',
        'test',
        'refactor',
        'chore',
        'ci',
        'build',
        'revert',
      ],
    ],
    'subject-case': [0],
    'subject-full-stop': [0],
    'header-max-length': [2, 'always', 120],
  },
};
