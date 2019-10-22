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
    new BundleTracker({filename: './apda/webpack-stats.json'}),
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
