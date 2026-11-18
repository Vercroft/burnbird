#!/bin/sh
# Every lint suppression names one rule and gives a reason of 10 or more characters, and
# .suppressions lists each one (path: comment, sorted, duplicates kept). A new or changed
# suppression therefore changes .suppressions in the same pull request.
set -eu
export LC_ALL=C
found=$(mktemp)
trap 'rm -f "$found"' EXIT

git ls-files -z -- '*.py' '*.pyi' '*.ts' '*.tsx' '*.js' '*.jsx' '*.mjs' '*.cjs' '*.mts' '*.cts' \
  '*.astro' '*.vue' '*.svelte' '*.rs' '*.go' '*.yml' '*.yaml' | xargs -0 -r perl -e '
my $bad = 0;
my $why = qr/\S.{9,}/;    # the reason: 10 or more characters
sub refuse { my ($p, $n, $c) = @_; warn "$p:$n: refused: $c\n"; $bad = 1 }
for my $p (@ARGV) {
  open my $fh, "<", $p or die "$p: $!\n";
  my $text = do { local $/; <$fh> };
  if ($p =~ /\.go$/) {
    # Generated files carry the Go header and are regenerated, never suppressed by hand.
    next if $text =~ m{^// Code generated .* DO NOT EDIT\.$}m;
    my $n = 0;
    for my $line (split /\n/, $text) {
      $n++;
      next unless $line =~ m{((?://\s*(?:nolint\b|lint:(?:file-)?ignore\b)|#nosec\b).*)$};
      my $c = $1;
      # Only at the end of a code line: at the start of a line it covers a block or the file.
      if ($line =~ m{^\s*(?!//)\S.*?\s(//nolint:(?!all\b)[a-z0-9]+ // $why)$}) {
        print "$p: $1\n";
      } else { refuse($p, $n, $c) }
    }
    next;
  }
  if ($p =~ /\.rs$/) {
    # Attributes may span lines (rustfmt splits long ones), so read the whole file.
    while ($text =~ /#(!?)\[((?:[^\]"]|"(?:[^"\\]|\\.)*")*)\]/g) {
      my ($inner, $body, $at) = ($1, $2, $-[0]);
      next unless $body =~ /\b(?:allow|expect)\s*\(/;
      my $n = 1 + (substr($text, 0, $at) =~ tr/\n//);
      (my $flat = $body) =~ s/\s+/ /g;
      if (!$inner && $flat =~ /^ ?expect\( ?([a-z_]+(?:::[a-z_]+)?) ?, ?reason ?= ?"((?:[^"\\]|\\.){10,})" ?,? ?\) ?$/) {
        print "$p: #[expect($1, reason = \"$2\")]\n";
      } else { refuse($p, $n, "#$inner\[$flat\]") }
    }
    next;
  }
  my $n = 0;
  for my $line (split /\n/, $text) {
    $n++;
    if ($p =~ /\.ya?ml$/) {
      next unless $line =~ /(#\s*zizmor\s*:.*)$/;
      my $c = $1;
      if ($c =~ /^#\s*zizmor:\s*ignore\[[a-z0-9-]+\]\s+#\s+$why$/) {
        print "$p: $c\n";
      } else { refuse($p, $n, $c) }
    } elsif ($p =~ /\.pyi?$/) {
      next unless $line =~ /(#\s*(?:noqa|ruff\s*:|type\s*:\s*ignore|mypy\s*:|pyright\s*:).*)$/;
      my $c = $1;
      if ($c =~ /^#\s*noqa:\s*[A-Z]+[0-9]+\s+#\s+$why$/ || $c =~ /^#\s*type:\s*ignore\[[a-z-]+\]\s+#\s+$why$/) {
        print "$p: $c\n";
      } else { refuse($p, $n, $c) }
    } else {
      next unless $line =~ /((?:\/\/|\/\*|<!--)\s*(?:biome-ignore|(?:eslint|oxlint)-(?:disable|enable)|prettier-ignore|\@ts-(?:ignore|nocheck|expect-error)).*)$/;
      (my $c = $1) =~ s/\s*(?:\*\/\}?|-->)\s*$//;
      if ($c =~ /^(?:\/\/|\/\*|<!--)\s*(?:biome-ignore lint\/[A-Za-z0-9]+\/[A-Za-z0-9]+: $why|(?:eslint|oxlint)-disable-next-line [\w\@\/.-]+ -- $why|\@ts-expect-error -- $why)$/) {
        print "$p: $c\n";
      } else { refuse($p, $n, $c) }
    }
  }
}
exit $bad;
' > "$found"

if ! sort "$found" | diff -u .suppressions -; then
  echo "check-suppressions: .suppressions does not match the code (diff above)" >&2
  exit 1
fi
