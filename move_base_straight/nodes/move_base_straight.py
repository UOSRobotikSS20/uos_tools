#! /usr/bin/env python

'''
Created on 01.08.2013

@author: Thorsten Gedicke
'''

import roslib; roslib.load_manifest('move_base_straight')
import rospy
import numpy as np
import tf
import actionlib
from geometry_msgs.msg import PoseStamped, Twist
from move_base_msgs.msg import MoveBaseAction, MoveBaseGoal
from sensor_msgs.msg import LaserScan

class MoveBaseStraightAction(object):

    FREQ = 10

    def __init__(self, name):
        # Get parameters -------------------------------------------------------
        # Max and min speeds
        self.MAX_SPEED = rospy.get_param('~max_speed', 0.2) # [m/s]
        self.MIN_SPEED = rospy.get_param('~min_speed', 0.05) # [m/s]

        # multiplier for turning ratio; e.g., 0.6 = rotate at 60% of the speed
        # required to fully turn towards the goal in 1 second
        self.ANGULAR_SPEED_GAIN = rospy.get_param('~angular_speed', 0.6)

        # Tolerance for goal approach (we stop when distance is below this)
        self.GOAL_THRESHOLD = rospy.get_param('~goal_threshold', 0.02) # [m]

        # Tolerance for movement success (action returns aborted if
        # distance to goal is still above this after we stopped)
        self.GOAL_THRESHOLD_ACCEPTABLE = rospy.get_param('~goal_threshold_acceptable', 0.2) # [m]

        self.YAW_GOAL_TOLERANCE = rospy.get_param('~yaw_goal_tolerance', 0.2) # [rad]

        # Minimal safety radius around base footprint
        self.RANGE_MINIMUM = rospy.get_param('~range_minimum', 0.5) # [m]

        # We start to slow down if distance to goal or obstacle distance to
        # RANGE_MINIMUM is below SLOWDOWN_RANGE.
        self.SLOWDOWN_RANGE = rospy.get_param('~slowdown_range', 0.5) # [m]

        # Scanned aperture around the goal for a movement to be cosidered safe.
        # At this time, there is no effect for this parameter since nothing
        # special is done for unsafe movements.
        self.REQUIRED_APERTURE = rospy.get_param('~required_aperture', np.pi * 0.75) # [rad]

        # Is the robot holonomic?
        self.HOLONOMIC = rospy.get_param('~holonomic', False)

        # We can also listen for PoseStamped targets on some topic.
        self.GOAL_TOPIC_NAME = rospy.get_param('~goal_topic_name', None)
        # ----------------------------------------------------------------------

        self.action_name = name
        self.tf_listener = tf.TransformListener()
        self.cmd_vel_pub = rospy.Publisher('base_controller/command', Twist, queue_size=1)

        self.speed_multiplier = 1.0

        # Subscribe to base_scan
        self.scan = None
        self.laser_base_offset = None    # will be initialized from first scan message
        rospy.Subscriber("/base_scan", LaserScan, self.laser_cb)

        self.footprint_frame = rospy.get_param('~footprint_frame', '/base_footprint')

        # Set up action server
        self.action_server = actionlib.SimpleActionServer(self.action_name, MoveBaseAction, execute_cb=self.execute_cb, auto_start=False)
        self.action_server.start()
        rospy.loginfo('%s: Action server up and running...' % (self.action_name))

        if self.GOAL_TOPIC_NAME:
            self.client = actionlib.SimpleActionClient('move_base_straight', MoveBaseAction)
            self.client.wait_for_server()
            rospy.Subscriber(self.GOAL_TOPIC_NAME, PoseStamped, self.manual_cb)
            rospy.loginfo('%s: Manual control topic is: %s' % (self.action_name, self.GOAL_TOPIC_NAME))

    def laser_to_base(self, distance_laser, angle_laser):
        """
        Uses the laws of sines and cosines to transform range/angle pairs in the
        base laser frame to range/angle pairs relative to the base footprint frame.
        """
        gamma = np.pi - angle_laser
        distance_base = np.sqrt(self.laser_base_offset**2 + distance_laser**2 - 2 * self.laser_base_offset * distance_laser * np.cos(gamma))
        angle_base = np.arcsin(distance_laser * np.sin(gamma) / distance_base)
        return (distance_base, angle_base)

    def blocked(self, target_angle, dist):
        # Something in the way?
        blocked = False
        block_reason = None
        # used to reduce speed for goal/obstacle approach
        self.speed_multiplier = min(1.0, (dist - self.GOAL_THRESHOLD) / self.SLOWDOWN_RANGE)
        laser_angle = self.scan.angle_min
        for laser_distance in self.scan.ranges:
            (base_distance, base_angle) = self.laser_to_base(laser_distance, laser_angle)
            angle_diff = abs(base_angle - target_angle)
            will_get_closer = angle_diff < (np.pi / 2.0)
            is_too_close = (base_distance < self.RANGE_MINIMUM) and (laser_distance > self.scan.range_min)
            if will_get_closer:
                self.speed_multiplier = min(self.speed_multiplier, (base_distance - self.RANGE_MINIMUM) / self.SLOWDOWN_RANGE)
            if is_too_close and will_get_closer:
                blocked = True
                block_reason = (base_distance, base_angle, angle_diff)
                break
            laser_angle += self.scan.angle_increment
        if blocked:
            rospy.logwarn('%s: Blocked! Reason: Distance %s at angle %s (%s angle difference to goal direction)' % (self.action_name, block_reason[0], block_reason[1], block_reason[2]))
        return blocked


    def translate_towards_goal_holonomically(self, x_diff, y_diff, dist):
        drive_speed = max(self.MAX_SPEED * self.speed_multiplier, self.MIN_SPEED)
        cmd = Twist()
        cmd.linear.x = (x_diff / dist) * drive_speed
        cmd.linear.y = (y_diff / dist) * drive_speed
        self.cmd_vel_pub.publish(cmd)


    def translate_towards_goal(self, target_angle):
        # Drive towards goal
        cmd = Twist()
        cmd.linear.x = max(self.MAX_SPEED * self.speed_multiplier, self.MIN_SPEED)
        cmd.angular.z = target_angle * self.ANGULAR_SPEED_GAIN
        self.cmd_vel_pub.publish(cmd)

    def rotate_in_place(self, direction):
        # Rotate towards goal orientation
        cmd = Twist()
        if (direction > 0):
            cmd.angular.z = 0.2
        else:
            cmd.angular.z = -0.2
        self.cmd_vel_pub.publish(cmd)

    def laser_cb(self, scan):
        if not self.laser_base_offset:
            # Get laser to base_footprint frame offset
            laser_frame = scan.header.frame_id
            while not rospy.is_shutdown():
                try:
                    self.tf_listener.waitForTransform(self.footprint_frame, laser_frame, rospy.Time(), rospy.Duration(1.0))
                    self.laser_base_offset = self.tf_listener.lookupTransform(self.footprint_frame, laser_frame, rospy.Time())[0][0]
                    break
                except (tf.Exception) as e:
                    rospy.logwarn("MoveBaseBlind tf exception! Message: %s" % e.message)
                    rospy.sleep(0.1)
                    continue

        self.scan = scan


    def manual_cb(self, target_pose):
        goal = MoveBaseGoal(target_pose=target_pose)
        self.client.send_goal(goal)

    def execute_cb(self, goal):
        if not self.scan or not self.laser_base_offset:
            rospy.logwarn('No messages received yet on topic %s, aborting goal!' % rospy.resolve_name('base_scan'))
            self.action_server.set_aborted()
            return

        # helper variables
        target_pose = goal.target_pose

        # publish info to the console
        rospy.loginfo('%s: Executing, moving to position: (%f, %f)' % (self.action_name, target_pose.pose.position.x, target_pose.pose.position.y))

        rate = rospy.Rate(hz = MoveBaseStraightAction.FREQ)
        while not rospy.is_shutdown():
            rate.sleep()
            # get transform relative to /base_footprint (--> target_pose_transformed)
            while not rospy.is_shutdown() and not self.action_server.is_preempt_requested():
                try:
                    # set the time stamp to now so we actually check for the
                    # same transform in waitForTransform and transformPose.
                    target_pose.header.stamp = rospy.Time.now()

                    self.tf_listener.waitForTransform('/base_footprint',
                            target_pose.header.frame_id,
                            target_pose.header.stamp, rospy.Duration(1.0))
                    target_pose_transformed = self.tf_listener.transformPose('/base_footprint', target_pose)
                    break
                except (tf.Exception) as e:
                    rospy.logwarn("MoveBaseBlind tf exception! Message: %s" % e.message)
                    rospy.sleep(0.1)
                    continue

            # check that preempt has not been requested by the client
            if self.action_server.is_preempt_requested():
                rospy.loginfo('%s: Preempted' % self.action_name)
                self.action_server.set_preempted()
                break

            x_diff = target_pose_transformed.pose.position.x
            y_diff = target_pose_transformed.pose.position.y
            dist = np.sqrt(x_diff ** 2 + y_diff ** 2)

            euler = tf.transformations.euler_from_quaternion([target_pose_transformed.pose.orientation.x,
                    target_pose_transformed.pose.orientation.y, target_pose_transformed.pose.orientation.z,
                    target_pose_transformed.pose.orientation.w])

            target_angle = np.arctan2(target_pose_transformed.pose.position.y,
                                      target_pose_transformed.pose.position.x)
            # target_angle is 0 in the center of the laser scan

            # Check if path is blocked. If so, abort.
            if self.blocked(target_angle, dist):
                # send command to stop
                self.cmd_vel_pub.publish(Twist())
                if(dist <= self.GOAL_THRESHOLD_ACCEPTABLE):
                    rospy.loginfo('%s: Succeeded (blocked, but closer to goal than acceptable goal threshold)' % self.action_name)
                    self.action_server.set_succeeded()
                else:
                    rospy.logwarn('%s: Aborted' % self.action_name)
                    self.action_server.set_aborted()
                break

            # Can we see enough?
            if ((target_angle - self.REQUIRED_APERTURE/2) < self.scan.angle_min or
                (target_angle + self.REQUIRED_APERTURE/2) > self.scan.angle_max):
                # Driving blind (maybe activate warning signals here)
                pass

            # Goal not yet reached?
            # Translate holonomically
            if (self.HOLONOMIC and dist > self.GOAL_THRESHOLD):
                self.translate_towards_goal_holonomically(x_diff, y_diff, dist)

            # Translate non-holonomically
            elif (dist > self.GOAL_THRESHOLD):
                self.translate_towards_goal(target_angle)

            # If arrived, but yaw error too large, stop and rotate
            elif (abs(euler[2]) > self.YAW_GOAL_TOLERANCE):
                self.rotate_in_place(euler[2])

            # Arrived
            else:
                # send command to stop
                self.cmd_vel_pub.publish(Twist())
                rospy.loginfo('%s: Succeeded' % self.action_name)
                self.action_server.set_succeeded()
                break

if __name__ == '__main__':
    rospy.init_node('move_base_straight')
    MoveBaseStraightAction(rospy.get_name())
    rospy.spin()
