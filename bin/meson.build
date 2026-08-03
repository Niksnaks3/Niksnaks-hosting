conf = configuration_data()
conf.set('PYTHON', python.full_path())
conf.set('VERSION', meson.project_version())
# Use absolute path for pkgdatadir
conf.set('pkgdatadir', get_option('prefix') / get_option('datadir'))

configure_file(
  input: 'niksnaks-hosting.in',
  output: 'niksnaks-hosting',
  configuration: conf,
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'rwxr-xr-x'
)
