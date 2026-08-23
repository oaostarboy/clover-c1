# nix/overlays.nix — Expose pkgs.clover-c1 for external NixOS configs
#
# The overlay is a pure alias for this flake's own package — NOT a
# re-instantiation against the consumer's nixpkgs.  This guarantees
# `pkgs.clover-c1`, `nix build .#default`, and the NixOS module's
# default package are all the exact same locked, tested derivation.
# (.override { extraPythonPackages = ...; } still works — callPackage's
# makeOverridable travels with the package.)
{ inputs, ... }:
{
  flake.overlays.default = final: _: {
    clover-c1 = inputs.self.packages.${final.stdenv.hostPlatform.system}.default;
  };
}
