from Spells import *
from Upgrades import *
from Level import *
from Shrines import *
from Mutators import *
from CommonContent import *
from Consumables import *
from Monsters import *
from RareMonsters import *
from Variants import *
from RiftWizard import *

import RiftWizard

import sys
curr_module = sys.modules[__name__]

EventOnShieldDamaged = namedtuple("EventOnShieldDamaged", "unit damage damage_type source")
EventOnHealed = namedtuple("EventOnHealed", "unit heal source")

class ChannelDependentBuff(Buff):

    def on_init(self):
        self.stack_type = STACK_INTENSITY
        self.passed = True
        self.owner_triggers[EventOnPass] = self.on_pass
        self.show_effect = False

    # Put this in pre-advance so that effects that happen when channeling stops
    # will benefit from stat buffs.
    def on_pre_advance(self):
        if not self.passed:
            self.owner.remove_buff(self)
        self.passed = False

    def on_pass(self, evt):
        self.passed = True

class DamageNegation:

    def __init__(self, evt, pay_costs=None, log=True):
        self.evt = evt
        self.pay_costs = pay_costs
        self.log = log
    
    def add_to_unit(self, unit):
        if hasattr(unit, "negates"):
            unit.negates.append(self)
        else:
            unit.negates = [self]

class EventOnPreDamagedPenetration:
    def __init__(self, evt, penetration, ignore_sh):
        self.unit = evt.unit
        self.damage = evt.damage
        self.damage_type = evt.damage_type
        self.source = evt.source
        self.penetration = penetration
        self.ignore_sh = ignore_sh

class EventOnDamagedPenetration:
    def __init__(self, evt, penetration, ignore_sh):
        self.unit = evt.unit
        self.damage = evt.damage
        self.damage_type = evt.damage_type
        self.source = evt.source
        self.penetration = penetration
        self.ignore_sh = ignore_sh

class EventOnMovedOldLocation:
    def __init__(self, evt, old_x, old_y):
        self.unit = evt.unit
        self.x = evt.x
        self.y = evt.y
        self.teleport = evt.teleport
        self.old_x = old_x
        self.old_y = old_y

class FreezeDependentBuff(Buff):

    def on_pre_advance(self):
        freeze = self.owner.get_buff(FrozenBuff)
        if freeze:
            self.turns_left = max(self.turns_left, freeze.turns_left)

    def on_unapplied(self):
        if self.turns_left <= 0:
            return
        buff = self.owner.get_buff(FrozenBuff)
        if buff:
            self.owner.apply_buff(self, self.turns_left)

class HydraBeam(BreathWeapon):

    def __init__(self, spell, caster, name, damage_type, dragon_mage_spell_type=None):
        self.spell = spell
        BreathWeapon.__init__(self)
        self.name = name
        self.damage = spell.get_stat("breath_damage")
        self.range = spell.get_stat("minion_range")
        self.damage_type = damage_type
        self.dragon_mage_spell = None
        if dragon_mage_spell_type:
            self.dragon_mage_spell = dragon_mage_spell_type()
            self.dragon_mage_spell.caster = caster
            self.dragon_mage_spell.owner = caster
            self.dragon_mage_spell.max_charges = 0
            self.dragon_mage_spell.cur_charges = 0
            self.dragon_mage_spell.range = RANGE_GLOBAL
            self.dragon_mage_spell.requires_los = False
            self.dragon_mage_spell.statholder = self.spell.caster
        self.description = "Beam attack. Counts as a breath weapon."
    
    def cast(self, x, y):
        for p in Bolt(self.caster.level, self.caster, Point(x, y)):
            self.per_square_effect(p.x, p.y)
        if self.dragon_mage_spell and self.spell.get_stat("dragon_mage"):
            self.caster.level.act_cast(self.caster, self.dragon_mage_spell, x, y, pay_costs=False)
        yield

    def get_impacted_tiles(self, x, y):
        return list(Bolt(self.caster.level, self.caster, Point(x, y)))

def drain_max_hp_kill(unit, hp, source):
    if hp <= 0:
        return 0
    if unit.shields:
        unit.shields -= 1
        unit.level.show_effect(unit.x, unit.y, Tags.Shield_Expire)
        return 0
    if unit.max_hp > hp:
        drain_max_hp(unit, hp)
        return hp
    else:
        old_hp = unit.max_hp
        unit.max_hp = 1
        unit.kill(damage_event=EventOnDamaged(unit, old_hp, Tags.Dark, source))
        return old_hp

def increase_cooldown(caster, target, melee):
    spells = [s for s in target.spells if s.cool_down and target.cool_downs.get(s, 0) < s.get_stat("cool_down")]
    if target.gets_clarity:
        spells = []
    if not spells:
        target.deal_damage(melee.get_stat("damage"), melee.damage_type, melee)
        return
    spell = random.choice(spells)
    cooldown = target.cool_downs.get(spell, 0)
    target.cool_downs[spell] = cooldown + 1

class RemoveBuffOnPreAdvance(Buff):

    def __init__(self, buff_class):
        self.buff_class = buff_class
        Buff.__init__(self)
        self.buff_type = BUFF_TYPE_PASSIVE
        self.stack_type = STACK_INTENSITY
    
    def on_attempt_apply(self, owner):
        for buff in owner.buffs:
            if isinstance(buff, RemoveBuffOnPreAdvance) and buff.buff_class == self.buff_class:
                return False
        return True

    def on_pre_advance(self):
        self.owner.remove_buff(self)

    def on_unapplied(self):
        for unit in list(self.owner.level.units):
            for buff in list(unit.buffs):
                if isinstance(buff, self.buff_class):
                    if hasattr(buff, "applier") and buff.applier is not self.owner:
                        continue
                    unit.remove_buff(buff)

class MinionBuffAura(Buff):

    def __init__(self, buff_class, qualifies, name, minion_desc):
        Buff.__init__(self)
        self.buff_class = buff_class
        self.qualifies = qualifies
        self.name = name
        example = self.buff_class()
        self.description = "All %s you summon gain %s for a duration equal to this buff's remaining duration." % (minion_desc, example.name)
        self.color = example.color
        self.global_triggers[EventOnUnitAdded] = self.on_unit_added
        self.buff_dict = defaultdict(lambda: None)
        self.advanced = False
        # Only used for when a unit is summoned while this buff has only 1 turn left.
        self.ignored_units = []

    def modify_unit(self, unit, duration):

        if are_hostile(self.owner, unit) or (unit is self.owner) or unit in self.ignored_units:
            return
        if not self.qualifies(unit):
            return
        
        if not unit.is_alive() and unit in self.buff_dict.keys():
            self.buff_dict.pop(unit)
            return

        if duration <= 0:
            self.ignored_units.append(unit)
            return
        
        if unit not in self.buff_dict.keys() or not self.buff_dict[unit] or not self.buff_dict[unit].applied:
            buff = self.buff_class()
            unit.apply_buff(buff, duration)
            self.buff_dict[unit] = buff

    def on_pre_advance(self):
        self.advanced = False

    def on_unit_added(self, evt):
        self.modify_unit(evt.unit, self.turns_left - (1 if not self.advanced else 0))
    
    def on_advance(self):
        self.advanced = True
        for unit in list(self.buff_dict.keys()):
            if not unit.is_alive():
                self.buff_dict.pop(unit)
        for unit in list(self.owner.level.units):
            if unit in self.ignored_units:
                continue
            self.modify_unit(unit, self.turns_left)
        self.ignored_units = []

def modify_class(cls):

    if cls is DeathChill:

        def get_impacted_tiles(self, x, y):
            return [Point(x, y)]

    if cls is MagnetizeSpell:

        def get_impacted_tiles(self, x, y):
            return [Point(x, y)]

    if cls is Unit:

        def deal_damage(self, amount, damage_type, spell, penetration=0, ignore_sh=False):
            if not self.is_alive():
                return 0
            return self.level.deal_damage(self.x, self.y, amount, damage_type, spell, penetration=penetration, ignore_sh=ignore_sh)

    if cls is Level:

        def deal_damage(self, x, y, amount, damage_type, source, flash=True, penetration=0, ignore_sh=False):

            # Auto make effects if none were already made
            if flash:
                effect = Effect(x, y, damage_type.color, Color(0, 0, 0), 12)
                if amount == 0:
                    effect.minor = True
                self.effects.append(effect)

            cloud = self.tiles[x][y].cloud
            if cloud and amount > 0:
                cloud.on_damage(damage_type)

            unit = self.get_unit_at(x, y)
            if not unit:
                return 0
            if not unit.is_alive():
                return 0


            # Raise pre damage event (for conversions)
            pre_damage_event = EventOnPreDamaged(unit, amount, damage_type, source)
            if penetration or ignore_sh:
                pre_damage_event = EventOnPreDamagedPenetration(pre_damage_event, penetration, ignore_sh)
            self.event_manager.raise_event(pre_damage_event, unit)

            # Factor in shields and resistances after raising the raw pre damage event
            resist_amount = unit.resists.get(damage_type, 0) - penetration

            # Cap effective resists at 100- shenanigans ensue if we do not
            resist_amount = min(resist_amount, 100)

            if resist_amount:
                multiplier = (100 - resist_amount) / 100.0
                amount = int(math.ceil(amount * multiplier))
            
            if hasattr(unit, "negates"):
                negates = [n for n in unit.negates if n.evt is pre_damage_event]
                if negates:
                    unit.negates = [n for n in unit.negates if n.evt is not pre_damage_event]
                    if amount > 0:
                        log = True
                        if not [n for n in negates if not n.pay_costs]:
                            negate = random.choice(negates)
                            negate.pay_costs()
                            if not negate.log:
                                log = False
                        if log:
                            self.combat_log.debug("%s negated %d %s damage from %s" % (unit.name, amount, damage_type.name, source.name))
                    return 0

            if amount > 0 and unit.shields > 0 and not ignore_sh:
                unit.shields = unit.shields - 1
                self.combat_log.debug("%s blocked %d %s damage from %s" % (unit.name, amount, damage_type.name, source.name))
                self.show_effect(unit.x, unit.y, Tags.Shield_Expire)				
                self.event_manager.raise_event(EventOnShieldDamaged(unit, amount, damage_type, source), unit)
                return 0

            amount = min(amount, unit.cur_hp)
            amount = max(unit.cur_hp - unit.max_hp, amount)
            # In case the unit is killed by a pre-damaged event triggered by a heal.
            if unit.is_alive():
                unit.cur_hp = unit.cur_hp - amount

            if amount > 0:
                self.combat_log.debug("%s took %d %s damage from %s" % (unit.name, amount, damage_type.name, source.name))
            elif amount < 0:
                self.combat_log.debug("%s healed %d from %s" % (unit.name, -amount, source.name))

            if (amount > 0):

                # Record damage sources when a player unit exists (aka not in unittests)
                if self.player_unit:
                    if are_hostile(unit, self.player_unit):
                        key = source.name
                        if not(isinstance(source, Buff) and source.buff_type == BUFF_TYPE_CURSE) and source.owner and source.owner.source:
                            key = source.owner.name

                        self.damage_dealt_sources[key] += amount
                    elif unit == self.player_unit:
                        if source.owner and not(isinstance(source, Buff) and source.buff_type == BUFF_TYPE_CURSE):
                            key = source.owner.name
                        else:
                            key = source.name	
                        self.damage_taken_sources[key] += amount

                damage_event = EventOnDamaged(unit, amount, damage_type, source)
                if penetration or ignore_sh:
                    damage_event = EventOnDamagedPenetration(damage_event, penetration, ignore_sh)
                self.event_manager.raise_event(damage_event, unit)
            
                if (unit.cur_hp <= 0):
                    unit.kill(damage_event = damage_event)		
                    
            # set amount to 0 if there is no unit- ie, if an empty tile or dead unit was hit
            else:
                if amount < 0:
                    self.event_manager.raise_event(EventOnHealed(unit, -amount, source), unit)
                amount = 0

            if (unit.cur_hp > unit.max_hp):
                unit.cur_hp = unit.max_hp

            return amount

        def act_move(self, unit, x, y, teleport=False, leap=False, force_swap=False):
            # Do nothing if something tries to move a dead unit- a spell or buff for instance
            if not unit.is_alive():
                return

            assert(isinstance(unit, Unit))
            
            if not leap:
                assert(self.can_move(unit, x, y, teleport=teleport, force_swap=force_swap))

            assert(unit.is_alive())

            if unit.is_player_controlled:
                prop = self.tiles[unit.x][unit.y].prop
                if prop:
                    prop.on_player_exit(unit)

            # flip sprite if needed
            if x < unit.x:
                unit.sprite.face_left = True
            if x > unit.x:
                unit.sprite.face_left = False

            # allow swaps
            swapper = self.tiles[x][y].unit

            oldx = unit.x
            oldy = unit.y

            self.tiles[unit.x][unit.y].unit = None
            unit.x = x
            unit.y = y

            self.tiles[x][y].unit = unit

            # allow swaps
            if swapper:
                self.tiles[oldx][oldy].unit = swapper
                swapper.x = oldx
                swapper.y = oldy
                self.event_manager.raise_event(EventOnMovedOldLocation(EventOnMoved(swapper, oldx, oldy, teleport=teleport), x, y), swapper)			

                # Fix perma circle on swap
                if swapper.is_player_controlled:
                    prop = self.tiles[swapper.x][swapper.y].prop
                    if prop:
                        prop.on_player_exit(swapper)


            self.event_manager.raise_event(EventOnMovedOldLocation(EventOnMoved(unit, x, y, teleport=teleport), oldx, oldy), unit)

            
            prop = self.tiles[x][y].prop
            if prop:
                if unit.is_player_controlled:
                    prop.on_player_enter(unit)
                prop.on_unit_enter(unit)

            cloud = self.tiles[x][y].cloud
            if cloud:
                cloud.on_unit_enter(unit)

    if cls is EventHandler:

        def raise_event(self, event, entity=None):
            event_type = type(event)
            if event_type == EventOnPreDamagedPenetration:
                event_type = EventOnPreDamaged
            elif event_type == EventOnDamagedPenetration:
                event_type = EventOnDamaged
            elif event_type == EventOnMoved:
                event = EventOnMovedOldLocation(event, event.x, event.y)
            elif event_type == EventOnMovedOldLocation:
                event_type = EventOnMoved
            # Record state of list once to ignore changes to the list caused by subscriptions
            if entity:
                for handler in list(self._handlers[event_type][entity]):
                    handler(event)
            global_handlers = list(self._handlers[event_type][None])
            for handler in global_handlers:
                handler(event)

    if cls is PyGameView:

        def draw_unit(self, u):
            x = u.x * SPRITE_SIZE
            y = u.y * SPRITE_SIZE

            if u.transform_asset_name:
                if not u.Transform_Anim:
                    u.Transform_Anim = self.get_anim(u, forced_name=u.transform_asset_name)
                u.Transform_Anim.draw(self.level_display)
            else:
                if not u.Anim:
                    u.Anim = self.get_anim(u)
                u.Anim.draw(self.level_display)

            # Friendlyness icon
            if not u.is_player_controlled and not are_hostile(u, self.game.p1):
                image = get_image(['friendly'])
                
                num_frames = image.get_width() // STATUS_ICON_SIZE
                frame_num = RiftWizard.cloud_frame_clock // STATUS_SUBFRAMES % num_frames 
                source_rect = (STATUS_ICON_SIZE*frame_num, 0, STATUS_ICON_SIZE, STATUS_ICON_SIZE)
                
                self.level_display.blit(image, (x + SPRITE_SIZE - 4, y+1), source_rect)

            # Lifebar
            if u.cur_hp != u.max_hp:
                hp_percent = u.cur_hp / float(u.max_hp)
                max_bar = SPRITE_SIZE - 2
                bar_pixels = int(hp_percent * max_bar)
                margin = (max_bar - bar_pixels) // 2
                pygame.draw.rect(self.level_display, (255, 0, 0, 128), (x + 1 + margin, y+SPRITE_SIZE-2, bar_pixels, 1))

            # Draw Buffs
            status_effects = []
            
            def get_buffs():
                seen_types = set()
                for b in u.buffs:
                    # Do not display icons for passives- aka, passive regeneration
                    if b.buff_type == BUFF_TYPE_PASSIVE and not hasattr(b, "show_icon"):
                        continue
                    if type(b) in seen_types:
                        continue
                    if b.asset == None:
                        continue
                    seen_types.add(type(b))
                    yield b

            status_effects = list(get_buffs())
            if not status_effects:
                return

            buff_x = x+1
            buff_index = RiftWizard.cloud_frame_clock // (STATUS_SUBFRAMES * 4) % len(status_effects)
            
            b = status_effects[buff_index]

            if not b.asset:
                color = b.color if b.color else Color(255, 255, 255)
                pygame.draw.rect(self.level_display, color.to_tup(), (buff_x, y+1, 3, 3))
            else:
                image = get_image(b.asset)
                num_frames = image.get_width() // STATUS_ICON_SIZE

                frame_num = RiftWizard.cloud_frame_clock // STATUS_SUBFRAMES % num_frames 
                source_rect = (STATUS_ICON_SIZE*frame_num, 0, STATUS_ICON_SIZE, STATUS_ICON_SIZE)
                self.level_display.blit(image, (buff_x, y+1), source_rect)
            buff_x += 4

    for func_name, func in [(key, value) for key, value in locals().items() if callable(value)]:
        if hasattr(cls, func_name):
            setattr(cls, func_name, func)

for cls in [Level, EventHandler, Unit, PyGameView, MagnetizeSpell, DeathChill]:
    curr_module.modify_class(cls)