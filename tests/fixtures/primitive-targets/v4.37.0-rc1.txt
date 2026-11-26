def primitiveTargets : M (Array Lean.Name) := do
  -- The challenge needs to have all the built-in constants of the kernel, as the
  -- kernel makes no guarantees when fed other definitions here.
  -- List from `git grep new_persistent_expr_const src/kernel/`
  return #[
    -- ``Nat.zero,
    -- ``Nat.succ,
    ``Nat.add,
    ``Nat.sub,
    ``Nat.mul,
    ``Nat.pow,
    ``Nat.gcd,
    ``Nat.div,
    ``Nat.mod,
    ``Nat.beq,
    ``Nat.ble,
    ``Nat.land,
    ``Nat.lor,
    ``Nat.xor,
    ``Nat.shiftLeft,
    ``Nat.shiftRight,
    ``String.ofList,
    ``Char.ofNat,
    ``List,
    ``eagerReduce,
    ``Nat,
    ``String,
    set_option linter.deprecated false in ``String.mk,
    ``Char,
    ``optParam,
    ``autoParam,
    ``semiOutParam,
    ``outParam
  ]

def builtinTargets : M (Array Lean.Name) := do
