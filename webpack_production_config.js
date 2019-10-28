var config = require('./webpack.config.js');
var BundleTracker = require('webpack-bundle-tracker');

config.output.path = require('path').resolve('./apda/static_root/webpack_bundles');

config.plugins = [
    new BundleTracker({filename: './webpack-stats-prod.json'})
]

// override any other settings here like using Uglify or other things that make sense for production environments.

module.exports = config;
