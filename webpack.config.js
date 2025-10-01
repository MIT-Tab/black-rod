const path = require("path");
const webpack = require("webpack");
const BundleTracker = require("webpack-bundle-tracker");
// eslint-disable-next-line import/no-extraneous-dependencies
const autoprefixer = require("autoprefixer");
// eslint-disable-next-line import/no-extraneous-dependencies
const sass = require("sass");

module.exports = {
  context: __dirname,

  entry: "./apda/assets/js/index",

  output: {
    path: path.resolve(__dirname, "apda/assets/webpack_bundles/"),
    filename: "[name]-[contenthash].js",
    clean: true,
  },

  plugins: [
    new BundleTracker({ filename: "webpack-stats.json" }),
    new webpack.ProvidePlugin({
      $: "jquery",
      jQuery: "jquery",
      Popper: ["popper.js", "default"],
    }),
  ],

  module: {
    rules: [
      {
        test: /\.css$/,
        use: ["style-loader", "css-loader"],
      },

      {
        test: /\.less$/,
        use: ["style-loader", "css-loader", "less-loader"],
      },

      {
        test: /\.scss$/,
        use: [
          "style-loader",
          "css-loader",
          {
            loader: "postcss-loader",
            options: {
              postcssOptions: {
                plugins: [autoprefixer],
              },
            },
          },
          {
            loader: "sass-loader",
            options: {
              implementation: sass,
            },
          },
        ],
      },

      {
        test: /\.jsx?$/,
        exclude: /node_modules/,
        use: {
          loader: "babel-loader",
          options: { presets: ["@babel/preset-env"] },
        },
      },
    ],
  },

  resolve: {
    extensions: [".js", ".jsx"],
  },

  devtool: "source-map",

  ignoreWarnings: [
    {
      module: /node_modules/,
    },
  ],
};
