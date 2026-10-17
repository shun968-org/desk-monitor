export default {
    extends: ['@commitlint/config-conventional'],
    rules: {
        'body-empty': [2, 'always'],
        'footer-empty': [2, 'always'],
        'references-empty': [2, 'never'],
    },
};
