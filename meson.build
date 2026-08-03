project('niksnaks-hosting',
  version: run_command('python3', '-c', 'import os; exec(open("niksnaks_hosting/version.py").read()); print(__version__)', check: true).stdout().strip(),
  meson_version: '>= 0.62.0',
)

python = import('python').find_installation('python3')

subdir('po')
subdir('bin')
subdir('packaging/linux')

# Install the niksnaks_hosting package directory to share/ (as /app/share/niksnaks_hosting)
install_subdir('niksnaks_hosting',
  install_dir: get_option('datadir'),
  strip_directory: false,
  exclude_files: ['meson.build']
)

# Install niksnaks_hosting.py to share/ (as /app/share/niksnaks_hosting.py)
install_data('niksnaks_hosting.py', install_dir: get_option('datadir'))
