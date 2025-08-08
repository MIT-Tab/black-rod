var path = require("path");
var webpack = require('webpack');
var BundleTracker = require('webpack-bundle-tracker');

module.exports = {
    context: __dirname,
    
    entry: './apda/assets/js/index',
    
    output: {
	path: path.resolve('./apda/assets/webpack_bundles/'),
	filename: "[name]-[hash].js",
    },
    
    plugins: [
	new BundleTracker({filename: 'webpack-stats.json'}),
	new webpack.ProvidePlugin({ // inject ES5 modules as global vars
	    $: 'jquery',
	    Popper: 'popper.js'
	})
    ],
    module: {
	rules: [
	    {
		test: /\.css/,
		use: [
		    "css-loader"
		]
	    },
	    {
		test: /\.(less)$/,
		use: [{
		    loader: 'style-loader' // creates style nodes from JS strings
		}, {
		    loader: 'css-loader' // translates CSS into CommonJS
		}, {
		    loader: 'less-loader' // compiles Less to CSS
		}],
	    },
	    {
		test: /\.(scss)$/,
		use: [{
		    loader: 'style-loader',
		}, {
		    loader: 'css-loader',
		}, {
		    loader: 'postcss-loader',
		    options: {
			plugins: function () {
			    return [
				require('precss'),
				require('autoprefixer')
			    ];
			}
		    }
		}, {
		    loader: 'sass-loader'
		}]
	    },
	    {
		test: /\.js$/,
		exclude: /node_modules/,
		use: ['babel-loader']
	    }
	]
    },
    resolve: {
	extensions: ['*', '.js', '.jsx']
    }
};
